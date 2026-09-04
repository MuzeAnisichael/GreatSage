$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python virtual environment creation failed.' }
}
& '.venv\Scripts\python.exe' -m pip install -e '.[dev,local]'
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies failed to install.' }
npm.cmd install
if ($LASTEXITCODE -ne 0) { throw 'Desktop dependencies failed to install.' }
Write-Output 'Setup complete. Run npm start to launch GreatSage.'
