# Builds dist\MeshCore-Desktop-windows-x64.zip. Run on Windows from the repo root:
#   powershell -ExecutionPolicy Bypass -File packaging\build-windows.ps1
# Needs Python 3.12 (python.org or `winget install Python.Python.3.12`).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
py -3.12 -m venv build\venv
build\venv\Scripts\python -m pip install -q -r requirements.txt pyinstaller
build\venv\Scripts\pyinstaller --noconfirm --clean --log-level WARN --distpath build\dist --workpath build\work packaging\meshcore-desktop.spec
$app = "build\dist\MeshCore Desktop"
Copy-Item packaging\PORTABLE.txt $app
New-Item -ItemType Directory -Force "$app\data" | Out-Null   # portable: keep messages beside the exe
New-Item -ItemType Directory -Force dist | Out-Null
Compress-Archive -Force -Path $app -DestinationPath dist\MeshCore-Desktop-windows-x64.zip
Write-Host "built dist\MeshCore-Desktop-windows-x64.zip"
