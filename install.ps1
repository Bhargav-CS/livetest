# livetest for Windows: irm https://raw.githubusercontent.com/Bhargav-CS/livetest/main/install.ps1 | iex
# Run it again to upgrade. $env:LIVETEST_REF picks a version (a release tag or a branch); default: the latest release.
& {  # a scope of its own: iex runs in the caller's session, which must keep its variables and settings
    $ErrorActionPreference = 'Stop'
    $repo = 'Bhargav-CS/livetest'
    $ref = $env:LIVETEST_REF
    if (-not $ref) {
        try { $ref = (irm "https://api.github.com/repos/$repo/releases/latest").tag_name }
        catch { throw "couldn't find livetest's latest release; set `$env:LIVETEST_REF (see https://github.com/$repo/releases)" }
    }
    if (Get-Command livetest -ErrorAction SilentlyContinue) { livetest ui stop }  # an upgrade: the running UI locks the files
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"  # its own process, as uv's docs run it
        $env:Path = "$HOME\.local\bin;$env:Path"  # where it installs uv; new terminals get it from the user's PATH
    }
    # --python: else uv picks its newest Python, which livekit-agents may have no wheels for yet (pyproject's requires-python)
    uv tool install --force --refresh-package livetest --python 3.13 "https://github.com/$repo/archive/$ref.zip"
    if ($LASTEXITCODE) { throw "uv couldn't install livetest $ref" }
    uv tool update-shell
    & "$(uv tool dir --bin)\livetest.exe" init  # by its path: this terminal's PATH may not have it yet
}
