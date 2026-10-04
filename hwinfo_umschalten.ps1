<#
  hwinfo_umschalten.ps1 - von LibreHardwareMonitor auf HWiNFO umschalten und danach zurueck

  LHM und HWiNFO lesen dieselben Sensoren (u. a. die RAM-Module ueber den SMBus). Laufen beide gleichzeitig,
  gibt es Lesefehler, z. B. blendet HWiNFO dann einen RAM-Sensor aus. Dieses Skript
    1. beendet LibreHardwareMonitor sauber (wie "Beenden" im Menue, nur im Notfall hart),
    2. startet HWiNFO und wartet, bis du HWiNFO schliesst,
    3. startet LibreHardwareMonitor wieder (ausser mit -OhneRueckkehr).
  Es fragt per UAC nach Adminrechten, weil beide Programme mit Adminrechten laufen.

  Normal ueber hwinfo_umschalten.bat starten (setzt zusaetzlich Markierungen fuer die Berichte).
  Desktop-Verknuepfung anlegen (einmalig, ohne Adminrechte):
    powershell -NoProfile -ExecutionPolicy Bypass -File hwinfo_umschalten.ps1 -VerknuepfungAnlegen

  Exitcodes: 0 = fertig, LHM laeuft wieder; 2 = fertig ohne Rueckkehr; 1 = abgebrochen/Fehler
#>
param(
    [string]$HWiNFO = 'C:\Program Files\HWiNFO64\HWiNFO64.EXE',
    [switch]$OhneRueckkehr,
    [switch]$VerknuepfungAnlegen
)
$ErrorActionPreference = 'Stop'

if ($VerknuepfungAnlegen) {
    $bat = Join-Path $PSScriptRoot 'hwinfo_umschalten.bat'
    $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'HWiNFO (LHM pausieren).lnk'
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = $bat
    $s.WorkingDirectory = $PSScriptRoot
    $s.WindowStyle = 7  # minimiert
    $s.Description = 'LibreHardwareMonitor sauber beenden, HWiNFO starten, danach LHM wieder starten'
    if (Test-Path -LiteralPath $HWiNFO) { $s.IconLocation = "$HWiNFO,0" }
    $s.Save()
    Write-Host "Verknuepfung angelegt: $lnk"
    exit 0
}

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    # sich selbst mit Adminrechten neu starten und auf das Ergebnis warten
    $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-HWiNFO', "`"$HWiNFO`"")
    if ($OhneRueckkehr) { $argv += '-OhneRueckkehr' }
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    try {
        $p = Start-Process -FilePath $ps -ArgumentList $argv -Verb RunAs -Wait -PassThru
    } catch {
        Write-Host 'Adminrechte abgelehnt - nichts geaendert.'
        exit 1
    }
    exit $p.ExitCode
}

if (-not (Test-Path -LiteralPath $HWiNFO)) {
    Write-Host "HWiNFO nicht gefunden: $HWiNFO"
    Write-Host 'Pfad mit -HWiNFO "C:\...\HWiNFO64.EXE" angeben. LHM bleibt unveraendert.'
    Start-Sleep -Seconds 8
    exit 1
}

# 1. LibreHardwareMonitor sauber beenden
$lhm = @(Get-Process -Name 'LibreHardwareMonitor' -ErrorAction SilentlyContinue)
$lhmPath = ($lhm | Where-Object { $_.Path } | Select-Object -First 1).Path
if ($lhm.Count) {
    Write-Host 'LibreHardwareMonitor wird beendet ...'
    $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    foreach ($p in $lhm) {
        # ohne /F: schickt "Fenster schliessen" - LHM speichert Einstellungen und schliesst das Log ordentlich
        & $taskkill /PID $p.Id 2>$null | Out-Null
    }
    $ids = $lhm.Id
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Process -Id $ids -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
    }
    $rest = @(Get-Process -Id $ids -ErrorAction SilentlyContinue)
    if ($rest.Count) {
        Write-Host 'LHM hat sich nicht selbst beendet (Option "Minimize On Close" aktiv?) - wird hart beendet.'
        $rest | Stop-Process -Force
        Start-Sleep -Seconds 1
    }
    Write-Host 'LibreHardwareMonitor beendet.'
} else {
    Write-Host 'LibreHardwareMonitor laeuft nicht.'
}

# 2. HWiNFO starten (oder ein bereits laufendes nutzen) und warten, bis es geschlossen wird
if (-not (Get-Process -Name 'HWiNFO64' -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $HWiNFO | Out-Null
}
if ($OhneRueckkehr) {
    Write-Host 'HWiNFO laeuft. LHM wird nicht wieder gestartet.'
    Start-Sleep -Seconds 3
    exit 2
}
Write-Host ''
Write-Host 'HWiNFO laeuft. Logging in HWiNFO bei Bedarf von Hand starten (Disketten-Symbol).'
Write-Host 'Dieses Fenster wartet, bis HWiNFO beendet wird, und startet dann LibreHardwareMonitor wieder.'
Start-Sleep -Seconds 5
while (Get-Process -Name 'HWiNFO64' -ErrorAction SilentlyContinue) {
    Start-Sleep -Seconds 2
}

# 3. LibreHardwareMonitor wieder starten
if ($lhmPath -and (Test-Path -LiteralPath $lhmPath)) {
    Write-Host 'HWiNFO beendet - LibreHardwareMonitor wird wieder gestartet.'
    Start-Process -FilePath $lhmPath -WorkingDirectory (Split-Path -Parent $lhmPath) | Out-Null
} else {
    Write-Host 'HWiNFO beendet. LHM lief vorher nicht - bitte bei Bedarf selbst starten.'
}
Start-Sleep -Seconds 3
exit 0
