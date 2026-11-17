@echo off
chcp 65001 >nul
rem 가짜 데이터로 실행. D200 이 없으면 시뮬레이터 창이 뜬다. (D200 없이 보려면: run-demo.bat --sim)
setlocal
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m charliedeck --demo %*
endlocal
