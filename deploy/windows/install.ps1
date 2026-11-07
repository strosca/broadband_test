<#
.SYNOPSIS
    Richtet linkmon unter Windows ein und registriert auf Wunsch den Dauerbetrieb.

.DESCRIPTION
    Ohne Schalter: virtuelle Umgebung anlegen, Paket installieren, config.toml anlegen.
    Bestehende Dateien bleiben erhalten. Dafür sind keine Administratorrechte nötig.

    -RegisterTask: zusätzlich eine Aufgabe in der Aufgabenplanung anlegen (Administratorrechte nötig).
    Sie startet beim Hochfahren, auch ohne Anmeldung, und startet nach einem Absturz neu.

    -UnregisterTask: nur diese Aufgabe wieder entfernen. Messdaten bleiben erhalten.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -RegisterTask
#>
[CmdletBinding()]
param(
    [switch]$RegisterTask,
    [switch]$UnregisterTask
)

$ErrorActionPreference = 'Stop'
$TaskName = 'linkmon'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Venv = Join-Path $Root '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
$Linkmon = Join-Path $Venv 'Scripts\linkmon.exe'
$Config = Join-Path $Root 'config.toml'

function Test-Administrator {
    $identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    return $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($UnregisterTask) {
    if (-not (Test-Administrator)) { throw 'Bitte PowerShell als Administrator starten.' }
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Aufgabe '$TaskName' entfernt. Die Messdaten bleiben erhalten."
    } else {
        Write-Host "Aufgabe '$TaskName' ist nicht vorhanden."
    }
    return
}

if (-not (Test-Path $Python)) {
    Write-Host 'Lege virtuelle Umgebung an ...'
    & python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtuelle Umgebung konnte nicht angelegt werden. Ist Python 3.11+ installiert?' }
}
Write-Host 'Installiere linkmon ...'
& $Python -m pip install --quiet --disable-pip-version-check -e $Root
if ($LASTEXITCODE -ne 0) { throw 'Installation fehlgeschlagen.' }

if (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $Root 'config.example.toml') $Config
    Write-Host "Konfiguration angelegt: $Config"
} else {
    Write-Host "Konfiguration bleibt unverändert: $Config"
}

if ($RegisterTask) {
    if (-not (Test-Administrator)) { throw 'Für -RegisterTask bitte PowerShell als Administrator starten.' }
    $Action = New-ScheduledTaskAction -Execute $Linkmon -Argument "--config `"$Config`" run" -WorkingDirectory $Root
    $Trigger = New-ScheduledTaskTrigger -AtStartup
    # S4U: läuft ohne Anmeldung unter deinem Konto, ohne dass ein Passwort gespeichert wird.
    # Dadurch gehören die Daten dir, und das Dashboard kann sie ohne Administratorrechte lesen.
    $User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal `
        -Settings $Settings -Description 'Dauermessung der Verfügbarkeit des Internetanschlusses (linkmon)' -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Aufgabe '$TaskName' registriert und gestartet."
}

Write-Host ''
Write-Host 'Nächste Schritte: siehe README.md'
Write-Host "  & '$Linkmon' --config '$Config' status"
Write-Host "  & '$Linkmon' --config '$Config' run"
Write-Host 'Webinterface während der Messung: http://127.0.0.1:8050'
