"""Console logging: warnings and up on stderr; everything with -v.

The root logger passes INFO on even when the console doesn't show it, so a test's own log handler (runtime) gets it.
"""

import logging


def setup(verbose: bool = False) -> None:
    console = logging.StreamHandler()  # stderr
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[console])
