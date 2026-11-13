# Install

[← README](../README.md)

## The one-liners

```sh
curl -LsSf https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.sh | sh     # macOS / Linux
```

```powershell
irm https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.ps1 | iex          # Windows
```

## What the script does

1. Finds the latest [release](https://github.com/Bhargav-CS/livetest/releases), unless you pick one (see below).
2. Stops livetest's background UI if it's running, so an upgrade can replace its files.
3. Installs [uv](https://docs.astral.sh/uv/) if you don't have it.
4. Runs `uv tool install` on that release's source code. uv downloads Python 3.13 for livetest, in its own environment, separate from any Python you have.
5. Puts `livetest` on your PATH (`uv tool update-shell`).
6. Runs `livetest init`, which creates `~/.livetest/` and prints the next steps.

Both scripts are short. Read them before you pipe them into a shell: [install.sh](../install.sh), [install.ps1](../install.ps1).

> [!NOTE]
> If `livetest` isn't found right after installing, open a new terminal.

## Pick a version

Set `LIVETEST_REF` to a tag from the [Releases](https://github.com/Bhargav-CS/livetest/releases) page, or to a branch:

```sh
curl -LsSf https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.sh | LIVETEST_REF=v0.1.0 sh
```

```powershell
$env:LIVETEST_REF = 'v0.1.0'; irm https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.ps1 | iex
```

Check what you have with `livetest --version`.

## Upgrade

Run the install line again. It stops the background UI first. If that UI started runs that are still going, they stop cleanly and keep what they recorded. Your agents, scenarios, plugins and results stay in `~/.livetest/`.

## Uninstall

```sh
livetest ui stop
uv tool uninstall livetest
```

That leaves your data. To remove it as well, delete `~/.livetest/`. It holds:

| | |
|---|---|
| `livetest.db` | agents, profiles, scenarios, plugins, runs and results |
| `.env` | your keys (plugins, and API keys of agents added with `--url`) |
| `recordings/` | voice test recordings |
| `errors.log` | tracebacks to attach to a bug report |
