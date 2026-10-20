param(
    [ValidateSet('dir', 'zip')]
    [string]$Target = 'dir'
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
& '.venv\Scripts\python.exe' -m PyInstaller --noconfirm greatsage-backend.spec
if ($LASTEXITCODE -ne 0) { throw 'Backend build failed. Install the build dependencies first.' }
& '.\node_modules\.bin\electron-builder.cmd' --win $Target
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
Write-Output 'Build complete: release\win-unpacked\GreatSage.exe'
if ($Target -eq 'zip') { Write-Output 'Distribution archive: release\*.zip' }
