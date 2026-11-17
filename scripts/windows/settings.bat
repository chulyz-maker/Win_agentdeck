@echo off
chcp 65001 >nul
rem CharlieDeck 설정 창 (계정·비밀번호·토큰·주소 입력)
setlocal
cd /d "%~dp0..\.."
start "" ".venv\Scripts\pythonw.exe" -m charliedeck --setup %*
endlocal
