<#
.SYNOPSIS
  Install (or remove) GateSense as a Windows service with NSSM, so it starts at boot.

.DESCRIPTION
  Optional. Runs "python -m api.run --no-build" from this folder as the service "GateSense" (the API and the web
  dashboard; build the dashboard first with start.bat or "npm run build" in web\). Output goes to
  data\logs\service.log (rotated at 5 MB). Stopping the service sends Ctrl+C, so the camera workers are stopped
  cleanly first.

  Needs NSSM (https://nssm.cc, nssm.exe) and an administrator PowerShell.

  Run it under your own Windows account (-Credential): the face models are in that account's profile
  (%USERPROFILE%\.insightface) and USB webcams may not be available to the built-in LocalSystem account. Without
  -Credential it runs as LocalSystem, and a warning says so.

.EXAMPLE
  .\scripts\install_service.ps1 -Nssm C:\Tools\nssm.exe -Credential (Get-Credential)

.EXAMPLE
  .\scripts\install_service.ps1 -Nssm C:\Tools\nssm.exe -Uninstall
#>
param(
    [string]$Nssm = "nssm",
    [System.Management.Automation.PSCredential]$Credential,
    [string]$Name = "GateSense",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this from an administrator PowerShell (right-click PowerShell, Run as administrator)."
}
$NssmPath = (Get-Command $Nssm -ErrorAction SilentlyContinue).Source
if (-not $NssmPath) { throw "nssm.exe not found ($Nssm). Download it from https://nssm.cc and pass -Nssm <path>." }

function Invoke-Nssm {
    & $NssmPath @args
    if ($LASTEXITCODE -ne 0) { throw "nssm $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

if ($Uninstall) {
    & $NssmPath stop $Name confirm 2>$null | Out-Null
    Invoke-Nssm remove $Name confirm
    Write-Host "Service $Name removed."
    exit 0
}

if (-not (Test-Path $VenvPython)) { throw "The Python environment is missing ($VenvPython): see Setup in README.md." }
if (-not (Test-Path (Join-Path $Root "web\dist\index.html"))) {
    throw "The web dashboard isn't built. Run start.bat once (or: cd web; npm install; npm run build), then try again."
}
# The venv's python.exe is a launcher that starts the real interpreter as a child; NSSM must watch the real one
# (the same reason app\workers.py starts workers this way).
$BasePython = (& $VenvPython -c "import sys; print(getattr(sys, '_base_executable', sys.executable))").Trim()
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force $LogDir | Out-Null

Invoke-Nssm install $Name $BasePython "-m api.run --no-build"
Invoke-Nssm set $Name AppDirectory $Root
Invoke-Nssm set $Name AppEnvironmentExtra "__PYVENV_LAUNCHER__=$VenvPython" "PYTHONUNBUFFERED=1"
Invoke-Nssm set $Name DisplayName "GateSense attendance"
Invoke-Nssm set $Name Description "GateSense face-recognition attendance: API, web dashboard and camera workers."
Invoke-Nssm set $Name Start SERVICE_DELAYED_AUTO_START
Invoke-Nssm set $Name AppStdout (Join-Path $LogDir "service.log")
Invoke-Nssm set $Name AppStderr (Join-Path $LogDir "service.log")
Invoke-Nssm set $Name AppRotateFiles 1
Invoke-Nssm set $Name AppRotateBytes 5000000
Invoke-Nssm set $Name AppStopMethodConsole 20000   # Ctrl+C, then up to 20 s to stop the cameras cleanly
Invoke-Nssm set $Name AppExit Default Restart
Invoke-Nssm set $Name AppRestartDelay 5000
if ($Credential) {
    Invoke-Nssm set $Name ObjectName $Credential.UserName $Credential.GetNetworkCredential().Password
} else {
    Write-Warning ("Running as LocalSystem: the face models would be downloaded again into the system profile, " +
                   "and USB webcams may not work. Re-run with -Credential (Get-Credential) to use your account.")
}
Invoke-Nssm start $Name
Write-Host "Service $Name installed and started. It starts at boot; logs: $LogDir\service.log"
