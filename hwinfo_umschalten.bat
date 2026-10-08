@echo off
rem hwinfo_umschalten - LibreHardwareMonitor sauber beenden, HWiNFO starten, danach LHM wieder starten.
rem LHM und HWiNFO lesen dieselben Sensoren; gleichzeitig gibt es Lesefehler (z. B. fehlende RAM-Sensoren).
rem Fragt per UAC nach Adminrechten. Setzt Markierungen, damit die Luecke im LHM-Log erklaert ist.
rem   hwinfo_umschalten.bat                 HWiNFO starten, nach dem Schliessen von HWiNFO wieder LHM
rem   hwinfo_umschalten.bat ohne-rueckkehr  LHM danach nicht wieder starten
setlocal
set "PY=py -3"
where py >nul 2>nul || set "PY=python"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
set "OPT="
if /i "%~1"=="ohne-rueckkehr" set "OPT=-OhneRueckkehr"
%PY% "%~dp0hwlog_check.py" --markieren "Umschalten: LHM beendet, HWiNFO gestartet" >nul 2>nul
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0hwinfo_umschalten.ps1" %OPT%
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" %PY% "%~dp0hwlog_check.py" --markieren "Umschalten: HWiNFO beendet, LHM wieder gestartet" >nul 2>nul
if "%RC%"=="1" %PY% "%~dp0hwlog_check.py" --markieren "Umschalten auf HWiNFO abgebrochen" >nul 2>nul
exit /b %RC%
