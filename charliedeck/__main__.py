"""CharlieDeck 실행.

    CharlieDeck                         D200 에 연결 (연결될 때까지 5초마다 다시 시도). 처음이면 설정 창부터.
    CharlieDeck --setup                 설정 창만 열기
    CharlieDeck --demo                  가짜 데이터 (D200 있으면 장치, 없으면 시뮬레이터 창)
    CharlieDeck --sim                   실제 데이터 + 시뮬레이터 창 (D200 없이 연결 테스트)
    CharlieDeck --demo --sim            가짜 데이터 + 시뮬레이터 창
    CharlieDeck --snapshot DIR          모든 화면 PNG 저장 (demo · 설정 전 두 가지)
    CharlieDeck --list-devices          D200 HID 경로 진단
    CharlieDeck --list-fonts            고른 글꼴 확인
    CharlieDeck --install-startup       Windows 로그인 때 자동 실행 (--uninstall-startup, --startup-status)

개발 중에는 `python -m charliedeck ...` 로 같은 옵션을 쓴다.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from . import settings as settings_mod
from .app import DeckApp
from .backends import Backend, HeadlessBackend
from .model import DemoProvider

log = logging.getLogger("charliedeck")
RECONNECT_SEC = 5
WATCH_SEC = 2.0  # 설정 · 비밀 값이 바뀌었는지 확인하는 간격
APP_LOCK_PORT = 47231


def setup_logging(verbose: bool, to_file: bool = True) -> Path | None:
    from platform_util import log_dir

    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    # exe(창 모드) · pythonw 에는 콘솔이 없어 sys.stdout 이 None → 파일 로그만
    if sys.stdout is not None and not any(type(h) is logging.StreamHandler for h in log.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    if not to_file:
        return None
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / "charliedeck.log"
        if not any(isinstance(h, RotatingFileHandler) for h in log.handlers):
            fh = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(fh)
        return path
    except OSError:
        return None


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%H:%M"):
        try:
            t = datetime.strptime(text, fmt)
            return datetime.combine(datetime.now().date(), t.time()) if fmt == "%H:%M" else t
        except ValueError:
            continue
    raise SystemExit(f"--at 형식: 2026-11-17T10:40 또는 10:40 (받은 값: {text})")


def open_vault():
    """자격 증명 관리자. 쓸 수 없으면 메모리 저장소 (경고)."""
    from .vault import MemoryBackend, Vault, VaultUnavailable

    try:
        return Vault()
    except VaultUnavailable as e:
        log.warning("%s — 비밀 값은 이번 실행 동안만 기억해요", e)
        return Vault(MemoryBackend())


def launch_setup(config: str | None = None) -> None:
    """설정 창을 별도 프로세스로 연다 (덱 · 시뮬레이터는 계속 돈다)."""
    from platform_util import no_window_flags

    from .startup import ROOT, command, frozen

    args = ["--setup"] + (["--config", config] if config else [])
    try:
        subprocess.Popen(command(None, args), creationflags=no_window_flags(), close_fds=True,
                         cwd=None if frozen() else str(ROOT))
    except OSError as e:
        log.error("설정 창을 열지 못했어요: %r", e)


def single_instance(port: int = APP_LOCK_PORT) -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return s
    except OSError:
        s.close()
        return None


# ---------- PNG 출력 ----------
def setup_needed_provider(settings):
    """'설정 전' 화면용: 비밀 값이 없는 실제 공급자 (네트워크에 접속하지 않음)."""
    from .live import LiveProvider
    from .vault import MemoryBackend, Vault

    p = LiveProvider(settings, Vault(MemoryBackend(), stamp=Path("/nonexistent/stamp")), start=False)
    for src in ("mail", "approval", "calendar", "todoist"):
        getattr(p, f"_poll_{src}")()
    return p


def snapshot(out_dir: str, at: datetime | None = None, config: str | None = None, demo_only: bool = False) -> list[Path]:
    """모든 페이지를 기기 모양 PNG 로 저장 + 칸별 PNG(keys/) + 요약 JSON. 장치 · 네트워크 불필요."""
    from .backends import frame_png
    from .pages import PAGES

    s = settings_mod.load(config) if config else settings_mod.Settings()
    now = at or datetime.now()
    out = Path(out_dir)
    (out / "keys").mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    summary: dict = {"version": __version__, "at": now.isoformat(timespec="minutes"), "modes": {}}
    modes = [("demo", "demo", DemoProvider())]
    if not demo_only:
        modes.append(("setup", "설정 전", setup_needed_provider(s)))
    for mode, label, provider in modes:
        app = DeckApp(s, provider, opener=lambda url: True, clock=lambda: now)
        summary["modes"][mode] = []
        for i, (pid, title) in enumerate(PAGES):
            app.go(i)
            keys, wide = app.frame(now)
            name = f"{mode}-{i + 1:02d}-{pid}"
            path = out / f"{name}.png"
            path.write_bytes(frame_png(keys, wide, f"{i + 1} · {title}  ({label})",
                                       f"CharlieDeck {__version__} · {now:%Y-%m-%d %H:%M} · 0~9 업무 · 10 이전 · 11 홈 · 12 다음 · "
                                       f"넓은 칸 길게 = 설정"))
            written.append(path)
            for k, data in keys.items():
                (out / "keys" / f"{name}-key{k:02d}.png").write_bytes(data)
            (out / "keys" / f"{name}-wide.png").write_bytes(wide)
            summary["modes"][mode].append({"id": pid, "title": title, "file": path.name,
                                           "keys": [{"index": k, "kind": key.kind, "label": key.label,
                                                     "value": key.value, "sub": key.sub}
                                                    for k, key in enumerate(app.keys(now))]})
        if mode == "demo":  # 할 일을 눌러 '완료' 가 된 모습
            app.go(0)
            app.tap(5)
            keys, wide = app.frame(now)
            path = out / "demo-06-todo-done.png"
            path.write_bytes(frame_png(keys, wide, "할 일 칸을 눌렀을 때 (demo)"))
            written.append(path)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return written


# ---------- 실행 루프 ----------
class Runner:
    def __init__(self, app: DeckApp, backend: Backend, vault=None, config: str | None = None) -> None:
        self.app, self.backend, self.vault, self.config = app, backend, vault, config
        self.stop = threading.Event()
        self.dirty = threading.Event()
        self.last_sig: tuple | None = None
        self.frames = 0
        self._watch_at = 0.0
        self._marks = self._stamp()
        backend.on_press = self._press
        backend.on_release = self._release
        app.on_brightness = backend.set_brightness
        app.on_change = self.dirty.set

    def _stamp(self) -> tuple[float, float]:
        return settings_mod.mtime(self.config), self.vault.stamp_mtime() if self.vault else 0.0

    def _press(self, idx: int) -> None:
        self.app.press(idx)

    def _release(self, idx: int) -> None:
        try:
            self.app.release(idx)
        except Exception as e:
            log.exception("button %s error: %r", idx, e)
        self.dirty.set()

    def watch(self) -> None:
        """셋업 창에서 저장하면 다시 읽기."""
        if time.monotonic() - self._watch_at < WATCH_SEC:
            return
        self._watch_at = time.monotonic()
        marks = self._stamp()
        if marks != self._marks:
            self._marks = marks
            s = settings_mod.load(self.config)
            for w in s.warnings:
                log.warning("설정: %s", w)
            self.app.reload_settings(s)
            log.info("설정을 다시 읽었어요")
            self.dirty.set()

    def tick(self) -> None:
        """여러 번 불러도 된다: 바뀐 게 있을 때만 새로 그려 보낸다."""
        self.watch()
        self.app.provider.poll()
        now = self.app.clock()
        sig = self.app.signature(now)
        if sig != self.last_sig or self.dirty.is_set():
            self.dirty.clear()
            keys, wide = self.app.frame(now)
            self.last_sig = self.app.signature(now)
            self.backend.show(keys, wide)
            self.frames += 1

    def run_threaded(self, seconds: float | None = None) -> None:
        self.backend.start()
        self.backend.set_brightness(self.app.brightness)
        t0 = time.monotonic()
        while not self.stop.is_set() and not self.backend.disconnected.is_set():
            try:
                self.tick()
            except Exception as e:
                log.exception("render error: %r", e)
            if seconds is not None and time.monotonic() - t0 >= seconds:
                break
            self.dirty.wait(0.25)


def make_device_backend() -> Backend:
    from .backends import D200Backend

    return D200Backend()


def run_device(app: DeckApp, vault, config: str | None, once: bool = False) -> int:
    """D200 에 연결해서 돈다. 끊기거나 아직 없으면 RECONNECT_SEC 마다 다시 시도."""
    stop = threading.Event()

    def quit_(*_):
        stop.set()

    signal.signal(signal.SIGINT, quit_)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, quit_)
    warned = False
    while not stop.is_set():
        try:
            backend = make_device_backend()
        except RuntimeError as e:
            if once:
                log.error("%s", e)
                return 1
            if not warned:
                log.warning("%s — %s초마다 다시 시도합니다", e, RECONNECT_SEC)
                warned = True
            stop.wait(RECONNECT_SEC)
            continue
        warned = False
        log.info("D200 연결됨")
        runner = Runner(app, backend, vault, config)
        runner.stop = stop
        try:
            runner.run_threaded()
        finally:
            backend.close()
        if backend.disconnected.is_set() and not stop.is_set():
            log.warning("D200 연결이 끊겼어요 — 다시 연결을 기다립니다")
    return 0


def run_simulator(app: DeckApp, vault, config: str | None, scale: float) -> int:
    from .backends import SimulatorBackend

    try:
        sim = SimulatorBackend(scale=scale)
    except Exception as e:  # tkinter 없음 / 화면 없음
        log.error("시뮬레이터 창을 열 수 없어요 (%r). --snapshot 으로 PNG 를 만들어 보세요", e)
        return 2
    runner = Runner(app, sim, vault, config)
    sim.on_tick = runner.tick
    sim.on_setup = lambda: launch_setup(config)
    sim.set_brightness(app.brightness)
    log.info("시뮬레이터 실행 중 — 창을 닫거나 Esc 로 종료")
    sim.run_mainloop()
    return 0


def run_headless(app: DeckApp, vault, config: str | None, seconds: float) -> int:
    runner = Runner(app, HeadlessBackend(), vault, config)
    runner.run_threaded(seconds)
    log.info("headless: %s초 동안 %s 프레임", seconds, runner.frames)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="CharlieDeck", description="CharlieDeck — Ulanzi D200 업무 대시보드 (Windows 11)")
    p.add_argument("--setup", action="store_true", help="설정 창 열기")
    p.add_argument("--demo", action="store_true", help="가짜 데이터")
    p.add_argument("--sim", action="store_true", help="D200 대신 시뮬레이터 창")
    p.add_argument("--headless", action="store_true", help="화면 없이 실행 (테스트용)")
    p.add_argument("--seconds", type=float, default=5.0, help="--headless 실행 시간 (초)")
    p.add_argument("--snapshot", metavar="DIR", help="모든 화면을 PNG 로 저장하고 끝내기")
    p.add_argument("--at", metavar="TIME", help="--snapshot 기준 시각 (예: 2026-11-17T10:40)")
    p.add_argument("--config", metavar="FILE", help="설정 파일 (기본: %%APPDATA%%\\CharlieDeck\\charliedeck.json)")
    p.add_argument("--scale", type=float, default=0.75, help="시뮬레이터 크기 배율")
    p.add_argument("--once", action="store_true", help="D200 이 없으면 기다리지 않고 끝내기")
    p.add_argument("--no-setup", action="store_true", help="처음 실행이어도 설정 창을 띄우지 않음")
    p.add_argument("--list-devices", action="store_true", help="D200 HID 경로 진단")
    p.add_argument("--list-fonts", action="store_true", help="사용할 글꼴 확인")
    p.add_argument("--install-startup", action="store_true", help="Windows 시작프로그램에 등록")
    p.add_argument("--uninstall-startup", action="store_true", help="Windows 시작프로그램에서 해제")
    p.add_argument("--startup-status", action="store_true", help="시작프로그램 등록 여부")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"CharlieDeck {__version__}")
    a = p.parse_args(argv)

    if a.setup:
        from .setup_ui import run

        return run(a.config)
    if a.list_fonts:
        import fonts

        for k, v in fonts.describe().items():
            print(f"{k:9s} {v}")
        return 0
    if a.list_devices:
        from d200 import list_devices, pick_interface

        try:
            devs = list_devices()
        except RuntimeError as e:
            print(e)
            return 1
        if not devs:
            print("D200(VID 2207 / PID 0019)을 찾지 못했어요. USB 케이블과 Ulanzi Studio 종료를 확인하세요.")
            return 1
        for d in devs:
            print(json.dumps(d, ensure_ascii=False))
        chosen = pick_interface([{**d, "usage_page": int(d["usage_page"], 16)} for d in devs])
        print("사용할 경로:", chosen["path"] if chosen else "-")
        return 0
    if a.install_startup or a.uninstall_startup or a.startup_status:
        from . import startup

        try:
            if a.install_startup:
                print("등록됨:", startup.install(["--demo"] if a.demo else []))
            elif a.uninstall_startup:
                print("해제됨" if startup.uninstall() else "등록되어 있지 않아요")
            else:
                print(startup.status())
        except RuntimeError as e:
            print(e)
            return 1
        return 0
    if a.snapshot:
        setup_logging(a.verbose, to_file=False)
        for f in snapshot(a.snapshot, parse_time(a.at), a.config):
            print(f)
        return 0

    logfile = setup_logging(a.verbose)
    lock = single_instance()
    if lock is None and not a.headless:
        log.error("CharlieDeck 이 이미 실행 중이에요 (설정은 넓은 칸 길게 또는 CharlieDeck --setup)")
        return 1
    s = settings_mod.load(a.config)
    for w in s.warnings:
        log.warning("설정: %s", w)
    vault = None
    if a.demo:
        provider = DemoProvider()
    else:
        from .live import LiveProvider

        vault = open_vault()
        if not s.exists and not a.no_setup and not a.headless:  # 처음 실행: 설정 창부터
            log.info("처음 실행이라 설정 창을 엽니다")
            launch_setup(a.config)
        provider = LiveProvider(s, vault)
    app = DeckApp(s, provider)
    app.on_setup = lambda: launch_setup(a.config)
    log.info("CharlieDeck %s 시작 (%s, 설정: %s, 로그: %s)", __version__, "demo" if a.demo else "실제 데이터",
             s.source if s.exists else "아직 없음", logfile or "-")
    try:
        if a.headless:
            return run_headless(app, vault, a.config, a.seconds)
        if a.sim:
            return run_simulator(app, vault, a.config, a.scale)
        if a.demo:  # demo: D200 이 있으면 장치, 없으면 시뮬레이터
            try:
                backend = make_device_backend()
            except RuntimeError as e:
                log.info("%s → 시뮬레이터로 실행합니다", e)
                return run_simulator(app, vault, a.config, a.scale)
            backend.close()
        return run_device(app, vault, a.config, once=a.once)
    finally:
        provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
