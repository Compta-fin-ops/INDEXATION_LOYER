@echo off
rem Remplace la base de test par une copie fraiche de la base de production.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Lancer d'abord lancer_console.bat une fois.
  pause
  exit /b 1
)
echo Cette operation REMPLACE la base de test par une copie fraiche de la base de production.
echo Tout ce qui a ete saisi dans la base de test sera perdu.
echo La base de production n'est pas modifiee.
echo.
echo Fermer la console de test avant de continuer.
set REPONSE=
set /p REPONSE=Taper OUI pour continuer : 
if /i not "%REPONSE%"=="OUI" (
  echo Abandon, rien n'a ete change.
  pause
  exit /b 0
)
".venv\Scripts\python.exe" -m console --base data\cabinet.sqlite copier-vers-test --vers data\test\test.sqlite --remplacer
pause
