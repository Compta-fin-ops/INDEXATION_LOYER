@echo off
rem Met a jour la console des loyers depuis GitHub. Double-cliquer sur ce fichier.
rem Suppose une installation faite avec Git : voir docs\installation.md.
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1

where git >nul 2>nul
if errorlevel 1 (
  echo Git est introuvable. L'installer depuis https://git-scm.com/download/win puis relancer ce fichier.
  pause
  exit /b 1
)
if not exist ".git" (
  echo Ce dossier a ete installe a partir d'un ZIP : il ne peut pas se mettre a jour tout seul.
  echo Suivre la partie "Installer avec Git" de docs\installation.md, une fois pour toutes.
  pause
  exit /b 1
)

echo Avant de continuer : fermer la fenetre noire de la console si elle est ouverte.
pause

rem 1. Sauvegarde de la base avant toute chose
if exist "data\cabinet.sqlite" if exist ".venv\Scripts\python.exe" (
  echo Sauvegarde de la base...
  ".venv\Scripts\python.exe" -m console --base data\cabinet.sqlite sauvegarder || goto :erreur
)

rem 2. Le fichier des indices INSEE est celui du depot : une copie locale modifiee bloquerait la mise a jour
git checkout -- data/indices 2>nul

rem 3. Mise a jour. L'option ff-only refuse toute fusion : en cas de doute, rien n'est change.
for /f "delims=" %%v in ('git rev-parse --short HEAD') do set AVANT=%%v
echo Recuperation de la derniere version...
git pull --ff-only || goto :erreur
for /f "delims=" %%v in ('git rev-parse --short HEAD') do set APRES=%%v

rem 4. Dependances, au cas ou la nouvelle version en demande une de plus
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :erreur
  ".venv\Scripts\python.exe" -c "import console; print('Version installee :', console.__version__)"
)

echo.
if "%AVANT%"=="%APRES%" echo La console etait deja a jour.
if not "%AVANT%"=="%APRES%" echo Mise a jour effectuee. Les indices INSEE sont a jour eux aussi.
echo Relancer la console avec lancer_console.bat, puis recharger la page par Ctrl+F5.
pause
goto :eof

:erreur
echo.
echo La mise a jour n'a pas abouti. Rien n'a ete supprime : la console reste dans son etat precedent.
echo Copier le message ci-dessus pour obtenir de l'aide.
pause
exit /b 1
