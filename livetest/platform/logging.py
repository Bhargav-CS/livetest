"""Console logging: warnings and up on stderr, without tracebacks; everything, tracebacks too, with -v.
Errors and their tracebacks always go to the `errors` file, for bug reports.

The root logger passes INFO on even when the console doesn't show it, so a test's own log handler (runtime) gets it.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(levelname)s %(name)s: %(message)s"


class _NoTraceback(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"{record.levelname} {record.name}: {record.getMessage()}"


def setup(verbose: bool = False, errors: Path | None = None) -> None:
    console = logging.StreamHandler()  # stderr
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter(FORMAT) if verbose else _NoTraceback())
    console.addFilter(lambda r: getattr(r, "console", True))  # extra={"console": False}: the file only
    handlers = [console]
    if errors:
        file = RotatingFileHandler(errors, maxBytes=1_000_000, backupCount=1, encoding="utf-8", delay=True)
        file.setLevel(logging.ERROR)
        file.setFormatter(logging.Formatter("%(asctime)s " + FORMAT))
        handlers.append(file)
    # force: replaces the handlers of an earlier call (tests call main() many times)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request the tester's plugins make
