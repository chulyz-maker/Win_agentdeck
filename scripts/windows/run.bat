@echo off
chcp 65001 >nul
rem CharlieDeck 실행 (D200 연결 필요, 연결될 때까지 5초마다 다시 시도). 처음이면 설정 창부터. 종료: Ctrl+C
setlocal
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m charliedeck %*
endlocal
