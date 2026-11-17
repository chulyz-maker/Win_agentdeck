"""Windows 시작프로그램 등록.

방식: 사용자 시작프로그램 폴더에 실행 파일 하나를 둔다 (관리자 권한 필요 없음).
    %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\CharlieDeck.vbs

- .vbs 는 콘솔 창 없이 CharlieDeck 을 실행한다 (exe 로 묶었으면 CharlieDeck.exe, 아니면 pythonw.exe -m charliedeck).
- 장치가 끊기거나 아직 연결되지 않았으면 CharlieDeck 이 스스로 다시 연결을 시도하므로 감시 프로세스가 필요 없다.
- 해제: 같은 파일을 지운다 (--uninstall-startup).

명령
    python -m charliedeck --install-startup [--demo]
    python -m charliedeck --uninstall-startup
    python -m charliedeck --startup-status
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from platform_util import IS_WINDOWS

ENTRY_NAME = "CharlieDeck.vbs"
ROOT = Path(__file__).resolve().parent.parent


def startup_dir(env: dict[str, str] | None = None) -> Path:
    """사용자 시작프로그램 폴더 (shell:startup)."""
    env = os.environ if env is None else env
    appdata = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def entry_path(env: dict[str, str] | None = None) -> Path:
    return startup_dir(env) / ENTRY_NAME


def pythonw_for(python: str | None = None) -> str:
    """콘솔 창이 없는 pythonw.exe (가상환경이면 그 안의 것). 없으면 python 그대로."""
    python = python or sys.executable
    p = Path(python)
    cand = p.with_name("pythonw.exe") if p.name.lower() == "python.exe" else p
    return str(cand if cand.exists() or not IS_WINDOWS else p)


def _vbs_str(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def frozen() -> bool:
    """PyInstaller 로 묶은 exe 로 실행 중인지."""
    return bool(getattr(sys, "frozen", False))


def command(python: str | None = None, args: list[str] | None = None) -> list[str]:
    if frozen() and python is None:
        return [sys.executable, *(args or [])]
    return [pythonw_for(python), "-m", "charliedeck", *(args or [])]


def build_vbs(python: str, root: str | Path, args: list[str] | None = None, exe: bool = False) -> str:
    """시작프로그램 .vbs 내용. 창 없이(0) 실행하고 기다리지 않는다(False)."""
    args = args or []
    # 각 부분을 따옴표로 감싸 공백이 있는 경로(예: C:\Users\홍 길동\...)도 안전하게
    cmd_parts = [python, *args] if exe else [python, "-m", "charliedeck", *args]
    cmd = " ".join(f'""{a}""' if (" " in a or not a) else a for a in cmd_parts)
    return (
        "' CharlieDeck 자동 실행 (설정 창 또는 CharlieDeck --install-startup 이 만든 파일)\r\n"
        "' 지우면 자동 실행이 해제됩니다.\r\n"
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f"sh.CurrentDirectory = {_vbs_str(str(root))}\r\n"
        f'sh.Run "{cmd}", 0, False\r\n'
    )


def install(args: list[str] | None = None, python: str | None = None, env: dict[str, str] | None = None) -> Path:
    if not IS_WINDOWS and env is None:
        raise RuntimeError("Windows 에서만 시작프로그램에 등록할 수 있어요")
    target = entry_path(env)
    target.parent.mkdir(parents=True, exist_ok=True)
    # VBScript 파일은 ANSI 또는 UTF-16 LE(BOM) 만 읽는다 → 한글 경로를 위해 UTF-16
    cmd = command(python, args)
    exe = frozen() and python is None
    root = Path(sys.executable).parent if exe else ROOT
    target.write_text(build_vbs(cmd[0], root, cmd[1:] if exe else args, exe=exe), encoding="utf-16")
    return target


def uninstall(env: dict[str, str] | None = None) -> bool:
    target = entry_path(env)
    if target.exists():
        target.unlink()
        return True
    return False


def status(env: dict[str, str] | None = None) -> str:
    target = entry_path(env)
    return f"등록됨: {target}" if target.exists() else f"등록 안 됨 ({target})"
