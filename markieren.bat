@echo off
rem markieren - eigene Beobachtung mit Uhrzeit festhalten (Bildaussetzer, Fehlermeldung, Absturz, LED ...)
rem Doppelklick: fragt nach einer Notiz. Oder: markieren.bat Bildaussetzer auf dem Desktop
rem Die Markierung erscheint in den Berichten als Linie in den Diagrammen und im Verlauf.
setlocal
set "PY=py -3"
where py >nul 2>nul || set "PY=python"
set "NOTIZ=%*"
if not defined NOTIZ set /p "NOTIZ=Was ist passiert? "
if not defined NOTIZ set "NOTIZ=(ohne Text)"
set "NOTIZ=%NOTIZ:"=%"
%PY% "%~dp0hwlog_check.py" --markieren "%NOTIZ%"
timeout /t 3 >nul
