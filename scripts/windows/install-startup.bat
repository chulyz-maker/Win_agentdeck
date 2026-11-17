@echo off
chcp 65001 >nul
rem Windows 로그온 시 CharlieDeck 자동 실행 등록 (시작프로그램 폴더에 CharlieDeck.vbs 생성, 관리자 권한 불필요)
rem 해제: uninstall-startup.bat
setlocal
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" -m charliedeck --install-startup %*
endlocal
