@echo off
chcp 65001 >nul
rem CharlieDeck 설치: 가상환경(.venv) 만들고 패키지 설치. 저장소 폴더 어디서 실행해도 된다.
setlocal
cd /d "%~dp0..\.."
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo [오류] Python 3.11 이상을 설치하세요: https://www.python.org/downloads/windows/
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
echo.
echo 설치 완료. D200 없이 demo: scripts\windows\run-demo.bat --sim   /  설정: scripts\windows\settings.bat
endlocal
