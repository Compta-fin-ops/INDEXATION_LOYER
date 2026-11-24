@echo off
rem Lance la console sur la BASE DE TEST (data\test\test.sqlite), a l'adresse http://127.0.0.1:8001
rem Au premier lancement, la base de test est creee par copie de la base de production.
call "%~dp0lancer_console.bat" test
