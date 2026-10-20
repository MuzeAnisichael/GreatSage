<#
.SYNOPSIS
Opt-in acceptance of this repository's NSIS installer using synthetic data.
.DESCRIPTION
Run from a normal (not elevated) PowerShell after building the installer:
  .\scripts\smoke_installer.ps1 -Installer .\release\GreatSage-VERSION-win-x64-Setup.exe -ManualExit

The three launch checks pause for the operator to use GreatSage's existing
menu/tray Exit action. Closing the window only hides it. No process is killed.
This checks SAME-VERSION replacement, not a cross-version upgrade.

The installer writes its normal HKCU registration, user shortcuts and
%LOCALAPPDATA%\greatsage-updater\installer.exe; NSIS may also use TEMP.
Existing GreatSage registrations/processes/shortcuts/updater cache cause refusal.
Successful completion uninstalls the test app and checks those entries vanished;
only this run's hash-matching cached installer and then its empty directory are
removed. The isolated fixture and report remain in .runtime/installer-smoke/<run>/.
On failure, unknown or still-running state is preserved for manual inspection.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [switch]$ManualExit
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $ManualExit) { throw 'Pass -ManualExit and attend the three normal-exit checkpoints; no unattended quit hook is used.' }
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Windows is required.' }
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskPrincipal = New-Object Security.Principal.WindowsPrincipal($taskIdentity)
if ($taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Use a non-elevated shell: this check must exercise current-user installation.'
}
$taskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) { throw 'The repository Python environment is required for synthetic fixtures.' }
$taskPackage = Get-Content -LiteralPath (Join-Path $taskRoot 'package.json') -Raw | ConvertFrom-Json
if ($taskPackage.name -ne 'greatsage' -or $taskPackage.build.appId -ne 'io.greatsage.desktop' -or
    $taskPackage.build.productName -ne 'GreatSage' -or $taskPackage.build.nsis.PSObject.Properties['guid'] -or
    $taskPackage.build.nsis.perMachine -ne $false -or $taskPackage.build.nsis.deleteAppDataOnUninstall -ne $false) {
    throw 'Installer identity or data-retention configuration changed; review this acceptance script first.'
}

function Assert-WorkspacePath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if (-not $absolute.StartsWith($taskRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'A test path escaped this repository.'
    }
    $cursor = $absolute
    while ($cursor -and $cursor.StartsWith($taskRoot, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Refusing a junction/symlink in the installer test paths.'
            }
        }
        $cursor = Split-Path -Parent $cursor
    }
    return $absolute
}

$taskInputInstaller = (Resolve-Path -LiteralPath $Installer).Path
$taskInputInstaller = Assert-WorkspacePath $taskInputInstaller
if ([IO.Path]::GetExtension($taskInputInstaller) -ne '.exe') { throw 'Select the generated NSIS .exe installer.' }
$taskGuid = & $taskPython -c 'import uuid; print(uuid.uuid5(uuid.UUID("50e065bc-3134-11e6-9bab-38c9862bdaf3"), "io.greatsage.desktop"))'
$taskParsedGuid = [Guid]::Empty
if ($LASTEXITCODE -ne 0 -or -not [Guid]::TryParse([string]$taskGuid, [ref]$taskParsedGuid)) {
    throw 'Could not derive the electron-builder installation identity.'
}
$taskGuid = ([string]$taskGuid).Trim()
$taskRegistryPaths = @(
    "HKCU:\Software\$taskGuid",
    "HKLM:\Software\$taskGuid",
    "HKCU:\Software\WOW6432Node\$taskGuid",
    "HKLM:\Software\WOW6432Node\$taskGuid",
    "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$taskGuid",
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$taskGuid",
    "HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$taskGuid",
    "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$taskGuid"
)
$taskUserInstallKey = "HKCU:\Software\$taskGuid"
$taskUserUninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$taskGuid"
$taskShortcutPaths = @(
    (Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) 'GreatSage.lnk'),
    (Join-Path ([Environment]::GetFolderPath('Programs')) 'GreatSage.lnk'),
    (Join-Path ([Environment]::GetFolderPath('CommonDesktopDirectory')) 'GreatSage.lnk'),
    (Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'GreatSage.lnk')
)
foreach ($entry in @($taskRegistryPaths + $taskShortcutPaths)) {
    if (Test-Path -LiteralPath $entry) { throw "Existing GreatSage registration or shortcut found; refusing installation: $entry" }
}
# electron-builder AppInfo.updaterCacheDirName + CURRENT_APP_INSTALLER_FILE_NAME.
# This upstream-owned location is outside the isolated application data directory.
$taskLocalAppData = [IO.Path]::GetFullPath([Environment]::GetFolderPath('LocalApplicationData'))
$taskUpdaterCache = Join-Path $taskLocalAppData 'greatsage-updater'
$taskUpdaterInstaller = Join-Path $taskUpdaterCache 'installer.exe'
if (Test-Path -LiteralPath $taskUpdaterCache) { throw 'An existing GreatSage updater cache exists; refusing installation.' }
function Assert-NoGreatSageProcesses {
    $existing = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('GreatSage.exe', 'greatsage-backend.exe') -or
        ($_.Name -in @('electron.exe', 'python.exe', 'pythonw.exe') -and
            ($_.ExecutablePath -like '*\GreatSage\*' -or $_.CommandLine -match '(?i)(-m\s+greatsage\b|GreatSage\\desktop)'))
    })
    if ($existing.Count) { throw 'A GreatSage process exists; exit it normally before changing this installation.' }
}
Assert-NoGreatSageProcesses
foreach ($hive in @('HKCU:\Software', 'HKLM:\Software', 'HKCU:\Software\WOW6432Node', 'HKLM:\Software\WOW6432Node')) {
    $uninstallRoot = Join-Path $hive 'Microsoft\Windows\CurrentVersion\Uninstall'
    if (Test-Path -LiteralPath $uninstallRoot) {
        foreach ($key in Get-ChildItem -LiteralPath $uninstallRoot) {
            $display = Get-ItemPropertyValue -LiteralPath $key.PSPath -Name DisplayName -ErrorAction SilentlyContinue
            if ($display -like 'GreatSage*') { throw 'Another GreatSage uninstall entry exists; refusing installation.' }
        }
    }
}

$taskRun = Assert-WorkspacePath (Join-Path $taskRoot ('.runtime\installer-smoke\' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)))
$taskInstallDir = Assert-WorkspacePath (Join-Path $taskRun 'installed app')
$taskDataDir = Assert-WorkspacePath (Join-Path $taskRun 'application data')
$taskInstaller = Assert-WorkspacePath (Join-Path $taskRun 'GreatSage-Setup.exe')
New-Item -ItemType Directory -Path $taskRun, $taskDataDir -Force > $null
Copy-Item -LiteralPath $taskInputInstaller -Destination $taskInstaller
$taskMarker = 'installer-fixture-' + [Guid]::NewGuid().ToString('N')
$taskReportPath = Join-Path $taskRun 'result.json'
$taskReport = [ordered]@{
    success = $false; version = $taskPackage.version; scope = 'same-version replacement; manual normal exit'
    installer_sha256 = (Get-FileHash -LiteralPath $taskInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    run_directory = $taskRun; install_directory = $taskInstallDir; data_directory = $taskDataDir
    app_id = $taskPackage.build.appId; installer_guid = $taskGuid
    external_writes = [ordered]@{
        registry = @($taskUserInstallKey, $taskUserUninstallKey)
        shortcuts = $taskShortcutPaths
        updater_cache = $taskUpdaterInstaller
        nsis_temporary_files = 'NSIS manages temporary installer/uninstaller files in Windows TEMP.'
    }
    checks = @(); data_hash_snapshots = @(); cleanup_complete = $false
}
$taskCurrentStage = 'fixture'
$taskActiveApp = $null

function Record-Check([string]$Name) {
    $taskReport.checks += [ordered]@{ check = $Name; passed = $true }
    $taskReport | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $taskReportPath -Encoding UTF8
}
function Wait-Condition([scriptblock]$Condition, [int]$Seconds, [string]$Failure) {
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    do {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw $Failure
}
function Assert-OwnedUpdaterCache {
    $expectedDirectory = [IO.Path]::GetFullPath((Join-Path $taskLocalAppData 'greatsage-updater'))
    $expectedFile = [IO.Path]::GetFullPath((Join-Path $expectedDirectory 'installer.exe'))
    if ([IO.Path]::GetFullPath($taskUpdaterCache) -ne $expectedDirectory -or
        [IO.Path]::GetFullPath($taskUpdaterInstaller) -ne $expectedFile) {
        throw 'Updater cache cleanup target changed; inspect manually.'
    }
    foreach ($entry in @($taskLocalAppData, $taskUpdaterCache, $taskUpdaterInstaller)) {
        if (-not (Test-Path -LiteralPath $entry) -or
            ((Get-Item -LiteralPath $entry -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Expected updater cache is missing or redirected; automatic cleanup refused.'
        }
    }
    $entries = @(Get-ChildItem -LiteralPath $taskUpdaterCache -Force)
    if ($entries.Count -ne 1 -or $entries[0].PSIsContainer -or $entries[0].FullName -ne $expectedFile -or
        (Get-FileHash -LiteralPath $taskUpdaterInstaller -Algorithm SHA256).Hash.ToLowerInvariant() -ne $taskReport.installer_sha256) {
        throw 'Updater cache contains unknown or changed data; automatic cleanup refused.'
    }
}
function Remove-OwnedUpdaterCache {
    # No recursive deletion: the directory was absent before this explicit run.
    Assert-OwnedUpdaterCache
    Remove-Item -LiteralPath $taskUpdaterInstaller
    if (@(Get-ChildItem -LiteralPath $taskUpdaterCache -Force).Count) {
        throw 'Updater cache is no longer empty; preserving the directory.'
    }
    Remove-Item -LiteralPath $taskUpdaterCache
    Record-Check 'Only this run''s hash-matching installer cache file and empty directory removed'
}
function Invoke-Installer {
    Assert-WorkspacePath $taskInstallDir > $null
    Assert-NoGreatSageProcesses
    # NSIS /D must be the LAST argument and MUST NOT be quoted, even with spaces.
    # Start-Process joins ArgumentList; no shell command is assembled or evaluated.
    $process = Start-Process -FilePath $taskInstaller -ArgumentList @('/S', ("/D=$taskInstallDir")) -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(120000)) { throw 'Installer is still running; inspect it manually. No process was killed.' }
    $process.Refresh()
    if ($process.ExitCode -ne 0) { throw 'NSIS installation failed.' }
    if (-not (Test-Path -LiteralPath $taskUserInstallKey) -or -not (Test-Path -LiteralPath $taskUserUninstallKey)) {
        throw 'Expected current-user NSIS registration is missing.'
    }
    $location = (Get-ItemProperty -LiteralPath $taskUserInstallKey).InstallLocation
    if ([IO.Path]::GetFullPath($location).TrimEnd('\') -ne $taskInstallDir.TrimEnd('\')) {
        throw 'NSIS registered an unexpected install directory; automatic cleanup refused.'
    }
    $expectedUninstall = '"' + (Join-Path $taskInstallDir 'Uninstall GreatSage.exe') + '" /currentuser'
    if ((Get-ItemProperty -LiteralPath $taskUserUninstallKey).UninstallString -ne $expectedUninstall) {
        throw 'NSIS uninstall registration does not match this run.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $taskInstallDir 'GreatSage.exe'))) { throw 'Installed executable missing.' }
    Assert-OwnedUpdaterCache
}
function Get-DataHashes {
    $hashes = @{}
    foreach ($file in Get-ChildItem -LiteralPath $taskDataDir -File -Recurse) {
        if ($file.FullName -like "$taskDataDir\electron-profile\*") { continue }
        $name = $file.FullName.Substring($taskDataDir.Length + 1)
        $hashes[$name] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
    if (-not $hashes.ContainsKey('memory.sqlite3') -or -not $hashes.ContainsKey('settings.json')) {
        throw 'Fixture database/configuration missing.'
    }
    return $hashes
}
function Assert-DataHashes($Expected, [string]$Label) {
    $current = Get-DataHashes
    if ($current.Count -ne $Expected.Count) { throw "Data file set changed during $Label." }
    foreach ($name in $Expected.Keys) {
        if (-not $current.ContainsKey($name) -or $current[$name] -ne $Expected[$name]) { throw "Data content changed during $Label." }
    }
    $taskReport.data_hash_snapshots += [ordered]@{ stage = $Label; sha256 = $current }
    Record-Check "$Label preserves exact backend data hashes"
}
function Invoke-OwnedUninstall {
    Assert-WorkspacePath $taskInstallDir > $null
    if ($taskActiveApp -and -not $taskActiveApp.HasExited) { throw 'The test application is still running; automatic uninstall refused.' }
    Assert-NoGreatSageProcesses
    if (-not (Test-Path -LiteralPath $taskUserInstallKey)) { throw 'Owned installation registration missing; inspect manually.' }
    $registered = (Get-ItemProperty -LiteralPath $taskUserInstallKey).InstallLocation
    if ([IO.Path]::GetFullPath($registered).TrimEnd('\') -ne $taskInstallDir.TrimEnd('\')) {
        throw 'Installation ownership changed; refusing uninstall.'
    }
    $uninstaller = Assert-WorkspacePath (Join-Path $taskInstallDir 'Uninstall GreatSage.exe')
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) { throw 'Owned uninstaller missing.' }
    $process = Start-Process -FilePath $uninstaller -ArgumentList '/S' -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(120000)) { throw 'Uninstaller is still running; inspect manually. No process was killed.' }
    # NSIS may relaunch its uninstaller from TEMP. Check its resulting state too.
    Wait-Condition {
        -not (Test-Path -LiteralPath $taskUserInstallKey) -and
        -not (Test-Path -LiteralPath $taskUserUninstallKey) -and
        -not (Test-Path -LiteralPath (Join-Path $taskInstallDir 'GreatSage.exe'))
    } 30 'Uninstall did not finish removing its registration/program.'
    foreach ($entry in @($taskRegistryPaths + $taskShortcutPaths)) {
        if (Test-Path -LiteralPath $entry) { throw "Uninstall left an entry; inspect it manually: $entry" }
    }
    Record-Check 'Owned uninstall removes program, registration and shortcuts'
}
function Invoke-InstalledApp([string]$Label) {
    $connectionPath = Join-Path $taskDataDir 'connection.json'
    if (Test-Path -LiteralPath $connectionPath) { throw 'Stale connection file exists before startup; inspect the prior exit.' }
    $previousDataDir = [Environment]::GetEnvironmentVariable('GREATSAGE_DATA_DIR', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('GREATSAGE_DATA_DIR', $taskDataDir, 'Process')
        $script:taskActiveApp = Start-Process -FilePath (Join-Path $taskInstallDir 'GreatSage.exe') -PassThru -WindowStyle Hidden
    } finally {
        [Environment]::SetEnvironmentVariable('GREATSAGE_DATA_DIR', $previousDataDir, 'Process')
    }
    Wait-Condition {
        if ($taskActiveApp.HasExited) { throw 'Installed desktop exited during startup.' }
        Test-Path -LiteralPath $connectionPath
    } 40 'Installed backend did not publish readiness.'
    $connection = Get-Content -LiteralPath $connectionPath -Raw | ConvertFrom-Json
    if ($connection.baseUrl -notmatch '^http://127\.0\.0\.1:\d+$') { throw 'Unexpected local service address.' }
    $headers = @{ Authorization = 'Bearer ' + $connection.token }
    Wait-Condition {
        try { (Invoke-RestMethod -Uri ($connection.baseUrl + '/health') -TimeoutSec 2).ok -eq $true } catch { $false }
    } 15 'Installed local service did not become healthy.'
    $settings = Invoke-RestMethod -Uri ($connection.baseUrl + '/api/settings') -Headers $headers -TimeoutSec 5
    $memories = Invoke-RestMethod -Uri ($connection.baseUrl + '/api/memories') -Headers $headers -TimeoutSec 5
    $status = Invoke-RestMethod -Uri ($connection.baseUrl + '/api/status') -Headers $headers -TimeoutSec 5
    if ($settings.global_prompt -ne $taskMarker -or -not @($memories | Where-Object { $_.text -eq $taskMarker }).Count) {
        throw 'Installed application could not read the synthetic settings and memory.'
    }
    if ($status.listening) { throw 'Audio capture unexpectedly enabled.' }
    $ownedBackend = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'greatsage-backend.exe' -and $_.ParentProcessId -eq $taskActiveApp.Id -and
        $_.ExecutablePath -eq (Join-Path $taskInstallDir 'resources\backend\greatsage-backend.exe')
    } | Select-Object -ExpandProperty ProcessId)
    if ($ownedBackend.Count -ne 1) { throw 'Could not identify exactly one backend child owned by this launch.' }
    Record-Check "$Label reads synthetic settings/memory and keeps audio off"
    Write-Host "${Label}: use GreatSage's menu or tray Exit. Closing the window only hides it."
    Read-Host 'After choosing Exit, press Enter to verify normal shutdown' > $null
    if (-not $taskActiveApp.WaitForExit(15000)) { throw 'Manual normal exit was not completed; no process was killed.' }
    Wait-Condition {
        -not (Test-Path -LiteralPath $connectionPath) -and
        -not @(Get-CimInstance Win32_Process | Where-Object {
            $_.ProcessId -in $ownedBackend -and $_.ExecutablePath -eq (Join-Path $taskInstallDir 'resources\backend\greatsage-backend.exe')
        }).Count
    } 10 'Backend cleanup or connection-file removal was not confirmed.'
    if (-not (Select-String -LiteralPath (Join-Path $taskDataDir 'backend.log') -SimpleMatch 'GreatSage desktop stopped' -Quiet)) {
        throw 'Normal desktop shutdown marker missing.'
    }
    Record-Check "$Label manual normal exit and owned backend cleanup"
    $headers = $null; $connection = $null
}

try {
    $fixturePath = Join-Path $taskRun 'seed_fixture.py'
    @'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from greatsage.settings import SettingsStore
from greatsage.memory import MemoryStore
data, marker = Path(sys.argv[2]), sys.argv[3]
settings = SettingsStore(data)
patch = {"global_prompt": marker, "voice_enabled": False, "allow_proactive": False,
         "semantic_decisions": False, "microphone": False, "desktop_source": "none"}
for component in ("asr", "llm", "tts", "embedding"):
    patch[component] = {"provider": "openai", "base_url": "http://127.0.0.1:9/v1",
                        "model": "installer-fixture", "api_key_env": ""}
patch["embedding"]["enabled"] = False
settings.update(patch)
memory = MemoryStore(data)
memory.add_memory(marker)
memory.close()
'@ | Set-Content -LiteralPath $fixturePath -Encoding UTF8
    & $taskPython $fixturePath $taskRoot $taskDataDir $taskMarker
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic fixture creation failed.' }
    $taskCurrentStage = 'install'
    Invoke-Installer
    Record-Check 'First current-user installation registers the isolated directory'
    Invoke-InstalledApp 'First installation'
    $baseline = Get-DataHashes
    $taskCurrentStage = 'same-version replacement'
    Invoke-Installer
    Assert-DataHashes $baseline 'Same-version replacement'
    Invoke-InstalledApp 'Same-version replacement'
    $baseline = Get-DataHashes
    $taskCurrentStage = 'uninstall'
    Invoke-OwnedUninstall
    Assert-DataHashes $baseline 'Uninstall'
    $taskCurrentStage = 'reinstall'
    Invoke-Installer
    Assert-DataHashes $baseline 'Reinstall'
    Invoke-InstalledApp 'Reinstall'
    $baseline = Get-DataHashes
    $taskCurrentStage = 'final cleanup'
    Invoke-OwnedUninstall
    Assert-DataHashes $baseline 'Final uninstall'
    Remove-OwnedUpdaterCache
    $taskReport.cleanup_complete = $true
    $taskReport.success = $true
} catch {
    $taskReport.checks += [ordered]@{ check = $taskCurrentStage; passed = $false }
    $taskReport.failure = 'Stopped at ' + $taskCurrentStage + '; inspect preserved run directory and any remaining owned installation. No forced process cleanup was attempted.'
    Write-Warning $taskReport.failure
} finally {
    $taskReport | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $taskReportPath -Encoding UTF8
    Write-Output "Acceptance report: $taskReportPath"
}
if (-not $taskReport.success) { exit 1 }
