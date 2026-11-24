@echo off
rem Lance la console des loyers sur ce poste (Windows). Double-cliquer sur ce fichier.
rem Base de test : double-cliquer sur lancer_console_test.bat.
rem Premiere execution : cree l'environnement Python et installe les dependances (quelques minutes).
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1

set PY=py -3
where py >nul 2>nul
if errorlevel 1 set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python est introuvable. Installer Python 3.11 ou plus recent depuis https://www.python.org/downloads/
  echo en cochant "Add python.exe to PATH", puis relancer ce fichier.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Preparation de l'environnement, une seule fois...
  %PY% -m venv .venv || goto :erreur
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
)

rem Composants : installes au premier lancement, puis seulement si une mise a jour en demande de nouveaux
fc /b requirements.txt ".venv\requirements.installees.txt" >nul 2>nul
if errorlevel 1 (
  echo Installation des composants, quelques minutes la premiere fois...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :erreur
  copy /y requirements.txt ".venv\requirements.installees.txt" >nul
)

rem Base de production par defaut ; "lancer_console.bat test" ouvre la base de test, sur un autre port
set BASE=data\cabinet.sqlite
set PORT=8000
set NATURE=production
if /i "%~1"=="test" (
  set BASE=data\test\test.sqlite
  set PORT=8001
  set NATURE=test
)
title Console des loyers - %NATURE%
if "%NATURE%"=="test" (
  ".venv\Scripts\python.exe" -m console --base data\cabinet.sqlite preparer-test --vers "%BASE%" || goto :erreur
)
".venv\Scripts\python.exe" -c "from console import db; import sys; sys.exit(0 if db.connecter(r'%BASE%').execute('SELECT COUNT(*) FROM utilisateurs').fetchone()[0] else 1)"
if errorlevel 1 (
  echo.
  echo Aucun compte n'existe encore : creation du premier compte, administrateur.
  ".venv\Scripts\python.exe" -m console --base "%BASE%" utilisateur creer || goto :erreur
)

echo.
echo Console de %NATURE% demarree : http://127.0.0.1:%PORT%
echo Laisser cette fenetre ouverte. La fermer arrete la console.
start "" http://127.0.0.1:%PORT%
".venv\Scripts\python.exe" -m console --base "%BASE%" serve --port %PORT%
goto :eof

:erreur
echo.
echo Une etape a echoue. Le message ci-dessus indique laquelle.
pause
exit /b 1
