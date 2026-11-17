@echo off
chcp 65001 >nul
rem 실제 데이터(설정한 메일·결재·일정·할 일)를 D200 없이 시뮬레이터 창으로 확인
setlocal
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m charliedeck --sim %*
endlocal
