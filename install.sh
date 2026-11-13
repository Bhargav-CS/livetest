#!/bin/sh
# livetest for macOS / Linux: curl -LsSf https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.sh | sh
# Run it again to upgrade. LIVETEST_REF picks a version (a release tag or a branch); default: the latest release.
set -eu
repo=Bhargav-CS/livetest
ref=${LIVETEST_REF:-$(curl -fsSL "https://api.github.com/repos/$repo/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p')}
[ -n "$ref" ] || { echo "error: couldn't find livetest's latest release; set LIVETEST_REF (see https://github.com/$repo/releases)" >&2; exit 1; }
if command -v livetest >/dev/null; then livetest ui stop || true; fi  # an upgrade: the old UI would keep running old code
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"  # where it installs uv; new shells get it from the profile it edits
fi
# --python: else uv picks its newest Python, which livekit-agents may have no wheels for yet (pyproject's requires-python)
uv tool install --force --refresh-package livetest --python 3.13 "https://github.com/$repo/archive/$ref.zip"
uv tool update-shell
"$(uv tool dir --bin)/livetest" init  # by its path: this shell's PATH may not have it yet
