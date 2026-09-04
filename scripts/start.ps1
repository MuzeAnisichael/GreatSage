$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    throw 'Run scripts\setup.ps1 first to install the Python environment.'
}
if (-not (Test-Path -LiteralPath 'node_modules\electron\dist\electron.exe')) {
    throw 'Run scripts\setup.ps1 first to install the desktop dependencies.'
}
& 'node_modules\electron\dist\electron.exe' .
if ($LASTEXITCODE -ne 0) { throw 'GreatSage exited unexpectedly. See .runtime\backend.log.' }
