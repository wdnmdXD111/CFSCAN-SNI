$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3.10+ is required. Install Python and add it to PATH.'
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create virtual environment.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (-not (Test-Path -LiteralPath 'config.json')) {
    Copy-Item -LiteralPath 'config.example.json' -Destination 'config.json'
}
if (-not (Test-Path -LiteralPath 'ipinfo_tokens.txt')) {
    Copy-Item -LiteralPath 'ipinfo_tokens.txt.example' -Destination 'ipinfo_tokens.txt'
}
if (-not (Test-Path -LiteralPath 'speed_token.txt')) {
    Copy-Item -LiteralPath 'speed_token.txt.example' -Destination 'speed_token.txt'
}
Write-Host 'Setup complete. Edit config.json and ipinfo_tokens.txt, then run .\start.cmd'
