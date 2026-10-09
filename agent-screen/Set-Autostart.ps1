param(
    [ValidateSet('Enable','Disable','Status')][string]$Action = 'Status',
    [string]$StartupDirectory = [Environment]::GetFolderPath('Startup'),
    [ValidateSet(90,270)][int]$Rotation = 90,
    [switch]$Preview
)
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$startupRoot = [IO.Path]::GetFullPath($StartupDirectory)
$entryPath = Join-Path $startupRoot 'Agent Signal - DS339.lnk'
$powerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$startScript = Join-Path $PSScriptRoot 'Start-Screen.ps1'
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $startScript + '" -Rotation ' + $Rotation
$exists = Test-Path -LiteralPath $entryPath
$shell = New-Object -ComObject WScript.Shell
$owned = $false
if ($exists) {
    $existing = $shell.CreateShortcut($entryPath)
    $owned = ($existing.TargetPath -ieq $powerShellExe) -and $existing.Arguments.Contains('"' + $startScript + '"')
}
if ($Action -eq 'Status' -or $Preview) {
    [PSCustomObject]@{Action=$Action;Preview=[bool]$Preview;Installed=($exists -and $owned);Path=$entryPath;Target=$powerShellExe;Arguments=$arguments} | ConvertTo-Json
    exit 0
}
if ($exists -and -not $owned) { throw ('Refusing to overwrite a shortcut owned by another application: ' + $entryPath) }
if ($Action -eq 'Enable') {
    $pythonw = Join-Path $repo '.local\agent-screen-venv\Scripts\pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Start Agent Screen once to prepare its Python environment before enabling autostart.' }
    if (-not (Test-Path -LiteralPath $startScript)) { throw 'The screen launcher is missing.' }
    [IO.Directory]::CreateDirectory($startupRoot) | Out-Null
    $link = $shell.CreateShortcut($entryPath)
    $link.TargetPath = $powerShellExe
    $link.Arguments = $arguments
    $link.WorkingDirectory = $repo
    $link.WindowStyle = 7
    $link.Description = 'DS339 Agent Signal - start silently at Windows sign-in'
    $link.Save()
    $saved = $shell.CreateShortcut($entryPath)
    if ($saved.TargetPath -ine $powerShellExe -or $saved.Arguments -cne $arguments) { throw 'Autostart shortcut verification failed.' }
    Write-Output ('Enabled Windows sign-in startup: ' + $entryPath)
} elseif ($exists) {
    $backups = Join-Path $repo '.local\agent-screen-autostart'
    [IO.Directory]::CreateDirectory($backups) | Out-Null
    $backupPath = Join-Path $backups ((Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-Agent-Signal-DS339.lnk')
    Copy-Item -LiteralPath $entryPath -Destination $backupPath
    Write-Output ('Removing owned startup shortcut to disable autostart: ' + $entryPath)
    Write-Output ('Backup: ' + $backupPath)
    Remove-Item -LiteralPath $entryPath
} else {
    Write-Output 'Agent Screen autostart is already disabled.'
}
