param([ValidateSet(90,270)][int]$Rotation = 90, [switch]$PreviewOnly)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $repo '.local\agent-screen-venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
$dataRoot = Join-Path $repo '.local\agent-screen'
if (-not (Test-Path -LiteralPath $python)) {
    & python -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the screen Python environment.' }
    & $python -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install screen dependencies.' }
}
[IO.Directory]::CreateDirectory($dataRoot) | Out-Null
$script = Join-Path $PSScriptRoot 'screen.py'
$statusPath = Join-Path $dataRoot 'status.json'
if (Test-Path -LiteralPath $statusPath) {
    try {
        $screenStatus = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($screenStatus.running) {
            $existing = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$screenStatus.pid)
            if ($existing -and $existing.CommandLine -and $existing.CommandLine.Contains($script)) {
                Write-Output ('Agent Screen is already running: PID ' + $screenStatus.pid)
                exit 0
            }
        }
    } catch { Write-Warning 'Previous screen status is unavailable; checking the process mutex at startup.' }
}
$arguments = '"' + $script + '" --data-root "' + $dataRoot + '" run --rotation ' + $Rotation
if (-not $PreviewOnly) { $arguments += ' --usb' }
$process = Start-Process -FilePath $pythonw -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $dataRoot 'stdout.log') `
    -RedirectStandardError (Join-Path $dataRoot 'stderr.log')
Write-Output ('Agent Screen started: PID ' + $process.Id)
Write-Output ('Status: ' + (Join-Path $dataRoot 'status.json'))
Write-Output ('Preview: ' + (Join-Path $dataRoot 'frame.png'))
