# Run AfterWord on the home network: http://afterword.local (and port 8000
# for the recorder). Production server (waitress), logging to
# data/server.log. Started at logon by scripts/autostart.ps1; can also be
# run by hand:  powershell -ExecutionPolicy Bypass -File scripts\run_home.ps1
#
# These settings win over .env for this run only.
$ErrorActionPreference = "Stop"
$server = Split-Path -Parent $PSScriptRoot
Set-Location $server

$env:AFTERWORD_MDNS_NAME = "afterword"     # http://afterword.local (mdns.py)
$env:AFTERWORD_EXTRA_PORTS = "80"          # so the address needs no ":8000"
$env:AFTERWORD_LOG_FILE = "data/server.log"

# ffmpeg from winget, when it isn't on PATH (a logon task gets a bare PATH).
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $ff = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($ff) { $env:AFTERWORD_FFMPEG_PATH = $ff.FullName }
}

& "$server\.venv\Scripts\python.exe" serve.py
