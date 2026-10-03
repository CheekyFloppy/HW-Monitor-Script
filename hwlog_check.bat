@echo off
rem hwlog_check - HWiNFO- und LibreHardwareMonitor-Logs auswerten
rem CSV-Dateien oder einen Ordner mit Logs auf diese Datei ziehen.
rem Ordner (z. B. C:\Program Files\LibreHardwareMonitor): nur neue oder
rem          gewachsene Logs werden ausgewertet; ist nichts neu, oeffnet der Verlauf.
rem --admin: Windows fragt per UAC nach Administratorrechten, damit das Tool
rem          die Dump-Dateien in C:\Windows\LiveKernelReports lesen kann.
rem          "Nein" klicken ist ok - dann laeuft die Auswertung ohne diesen Teil.
setlocal
set "PY=py -3"
where py >nul 2>nul || set "PY=python"
if "%~1"=="" (
  echo CSV-Datei^(en^) oder einen Log-Ordner auf diese Datei ziehen.
  echo.
  pause
  exit /b 1
)
set "EXTRA="
if exist "%~1\*" set "EXTRA=--neu"
%PY% "%~dp0hwlog_check.py" --admin --oeffnen %EXTRA% %*
echo.
pause
