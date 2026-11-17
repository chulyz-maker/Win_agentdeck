@echo off
chcp 65001 >nul
rem 모든 화면을 PNG 로 저장 (장치·로그인 불필요). demo 화면 + 설정 전 화면 → snapshots 폴더
setlocal
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m charliedeck --snapshot snapshots %*
echo PNG 저장 위치: %cd%\snapshots
endlocal
