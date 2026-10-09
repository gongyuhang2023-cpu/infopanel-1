$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$python = Join-Path $repo '.local\agent-screen-venv\Scripts\python.exe'
& $python (Join-Path $PSScriptRoot 'screen.py') stop
if ($LASTEXITCODE -ne 0) { throw 'Could not request screen shutdown.' }
Write-Output 'Shutdown requested. The display will turn off and release USB.'
