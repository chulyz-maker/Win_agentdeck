"""Windows 11 도우미 (경로 · 브라우저 · 알림 · 프로세스).

- 데이터·로그 폴더: %LOCALAPPDATA%\\CharlieDeck   (개발용 리눅스: ~/.local/share/charliedeck)
- 사용자 설정 폴더:  %APPDATA%\\CharlieDeck        (개발용 리눅스: ~/.config/charliedeck)
- URL 열기: 평소 쓰는 Chrome → 없으면 Edge → 기본 브라우저
- 알림: PowerShell 토스트 (실패해도 무시)

외부 패키지 없이 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
APP_NAME = "CharlieDeck"

# Windows 에서 Chrome / Edge 가 흔히 설치되는 위치 (사용자 설치 → 시스템 설치 → Edge)
BROWSER_CANDIDATES = [
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
]


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _windows_paths(env) -> bool:
    return IS_WINDOWS or bool(env.get("CHARLIEDECK_FORCE_WINDOWS_PATHS"))


# ---------- 폴더 ----------
def data_dir(env: dict[str, str] | None = None) -> Path:
    """앱 데이터 폴더 (상태·로그·전용 Chrome 프로필). 경로만 돌려주고 만들지는 않는다."""
    env = os.environ if env is None else env
    if env.get("CHARLIEDECK_HOME"):
        return Path(env["CHARLIEDECK_HOME"])
    if _windows_paths(env):
        return Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    return Path.home() / ".local" / "share" / "charliedeck"


def config_dir(env: dict[str, str] | None = None) -> Path:
    """사용자 설정 폴더 (charliedeck.json). 비밀번호·토큰은 여기 두지 않는다 (Windows 자격 증명 관리자)."""
    env = os.environ if env is None else env
    if env.get("CHARLIEDECK_CONFIG_DIR"):
        return Path(env["CHARLIEDECK_CONFIG_DIR"])
    if _windows_paths(env):
        return Path(env.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    return Path.home() / ".config" / "charliedeck"


def log_dir() -> Path:
    return data_dir() / "logs"


# ---------- 프로세스 ----------
def no_window_flags() -> int:
    """콘솔 창을 띄우지 않고 자식 프로세스 실행 (Windows)."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def detached_flags() -> int:
    """부모(CharlieDeck)가 끝나도 살아 있는 독립 프로세스 (Windows)."""
    return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# ---------- 브라우저 · 파일 ----------
def find_chrome(configured: str = "") -> str | None:
    """설정한 경로 → Windows 기본 설치 위치(Chrome → Edge) → PATH 순으로 찾는다."""
    if configured and Path(_expand(configured)).is_file():
        return _expand(configured)
    if IS_WINDOWS:
        for c in BROWSER_CANDIDATES:
            p = _expand(c)
            if Path(p).is_file():
                return p
    for name in ("chrome", "msedge", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_url(url: str, chrome_path: str = "") -> bool:
    """평소 쓰는 Chrome 으로 연다 (없으면 기본 브라우저). http(s) 주소만 연다."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return False
    chrome = find_chrome(chrome_path)
    if chrome:
        try:
            subprocess.Popen([chrome, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=detached_flags())
            return True
        except OSError:
            pass
    try:
        return webbrowser.open(url, new=2)
    except webbrowser.Error:
        return False


def open_path(target: str) -> bool:
    """파일·폴더를 기본 프로그램으로 연다 (진단 파일 열기 등)."""
    if not target:
        return False
    try:
        if IS_WINDOWS:
            os.startfile(_expand(target))  # type: ignore[attr-defined]  # noqa: S606
            return True
        return subprocess.run(["xdg-open", _expand(target)], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ---------- 알림 ----------
def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def notify(title: str, message: str) -> None:
    """Windows 토스트 알림. 실패해도 예외를 내지 않는다."""
    if not IS_WINDOWS:
        return
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode({_ps_quote(title)})) > $null;"
        f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode({_ps_quote(message)})) > $null;"
        "$n = [Windows.UI.Notifications.ToastNotification]::new($t);"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier({_ps_quote(APP_NAME)}).Show($n)"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=no_window_flags())
    except OSError:
        pass
