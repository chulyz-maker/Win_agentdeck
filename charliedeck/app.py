"""CharlieDeck 상태 기계: 페이지 · 버튼 입력 · 화면 프레임.

장치(D200) · 시뮬레이터 · 헤드리스와 상관없이 같은 코드가 돈다.
- frame(now)  → ({0..12: 196x196 PNG}, 458x196 넓은 칸 PNG)
- press(i) / release(i) → 버튼 입력 (길게 누르기는 누른 시간으로 판단)

버튼 규칙
  0~9   업무 버튼 (칸 종류에 따라)
        메일: 메일함 열기 + 새 메일 수 0 (POP3)       결재: 결재함 열기 (로그인 필요면 로그인 창)
        일정: 다가오는 일정 보기                       이카운트: 업무 브라우저에서 자동 로그인
        할 일: 완료 (8초 안에 다시 누르면 취소)        설정 필요 칸: 설정 창 열기
  10    이전 페이지 (첫 페이지 → 마지막)
  11    짧게: 업무 홈 (홈에서는 모두 새로고침) / 길게: 밝기 20 → 50 → 80 → 100
  12    다음 페이지 (마지막 → 첫 페이지)
  13·14 넓은 칸: 짧게 = 다가오는 일정 3개 / 길게 = 설정 창
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import render
from .model import Snapshot
from .pages import NAV_HOME, NAV_NEXT, NAV_PREV, PAGE_COLOR, PAGES, SHORT_TITLE, WIDE_KEYS, Key, build, page_index
from .settings import Settings, is_work_url

log = logging.getLogger("charliedeck")

BRIGHTNESS_STEPS = (20, 50, 80, 100)


@dataclass
class Toast:
    title: str
    lines: tuple[str, ...]
    color: str
    until: float


class DeckApp:
    def __init__(self, settings: Settings, provider, opener: Callable[[str], bool] | None = None,
                 clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] | None = None) -> None:
        self.settings = settings
        self.provider = provider
        self.clock = clock or datetime.now
        self.mono = monotonic or time.monotonic
        if opener is None:
            from platform_util import open_url

            def opener(url: str) -> bool:
                return open_url(url, self.settings.chrome_path)

        self.opener = opener
        self.page = 0
        self.brightness = settings.brightness
        self.toast: Toast | None = None
        self.pressed: dict[int, float] = {}
        self.lock = threading.RLock()
        self.on_brightness: Callable[[int], None] | None = None  # 장치 밝기 (백엔드가 연결)
        self.on_setup: Callable[[], None] | None = None  # 설정 창 열기 (실행부가 연결)
        self.on_change: Callable[[], None] | None = None  # 백그라운드 작업이 끝나 다시 그려야 할 때
        self.actions: list[str] = []  # 한 일 기록 (테스트 · 로그)

    # ---------- 데이터 ----------
    @property
    def snap(self) -> Snapshot:
        return self.provider.get(self.clock())

    def reload_settings(self, settings: Settings) -> None:
        with self.lock:
            self.settings = settings
            self.brightness = settings.brightness
            self.provider.reload(settings)
            if self.on_brightness:
                self.on_brightness(self.brightness)
            self.show_toast("설정", ("새 설정을 적용했어요",), render.TEAL)

    # ---------- 화면 ----------
    def keys(self, now: datetime | None = None, snap: Snapshot | None = None) -> list[Key]:
        now = now or self.clock()
        return build(self.page, snap or self.provider.get(now), now, self.settings)

    def active_toast(self) -> Toast | None:
        for title, lines, color in self.provider.drain_notices():
            self.show_toast(title, lines, color, self.settings.toast_sec * 2)
        if self.toast and self.mono() < self.toast.until:
            return self.toast
        self.toast = None
        return None

    def signature(self, now: datetime) -> tuple:
        """화면이 바뀌었는지 판단하는 요약 (분 단위 시계 + 공급자 버전)."""
        self.provider.expire()
        t = self.active_toast()
        return (self.page, now.strftime("%Y%m%d%H%M"), self.brightness, self.provider.version,
                (t.title, t.lines) if t else None)

    def frame(self, now: datetime | None = None) -> tuple[dict[int, bytes], bytes]:
        with self.lock:
            now = now or self.clock()
            snap = self.provider.get(now)
            out: dict[int, bytes] = {}
            for i, k in enumerate(self.keys(now, snap)):
                out[i] = tile(k)
            n = len(PAGES)
            title = PAGES[self.page][1]
            out[NAV_PREV] = render.nav_tile("prev", self.page + 1, n, title, SHORT_TITLE[PAGES[(self.page - 1) % n][0]])
            out[NAV_HOME] = render.nav_tile("home", self.page + 1, n, title, brightness=self.brightness)
            out[NAV_NEXT] = render.nav_tile("next", self.page + 1, n, title, SHORT_TITLE[PAGES[(self.page + 1) % n][0]])
            t = self.active_toast()
            cal = snap.state("calendar")
            ok = lambda src, v: v if snap.state(src).status == "ok" else None  # noqa: E731
            left = sum(1 for x in snap.todos if not x.done)
            wide = render.wide_panel(
                now, tuple(SHORT_TITLE[p] for p, _ in PAGES), self.page, snap.next_event(now),
                calendar=(cal.status, cal.message), demo=snap.demo,
                toast=(t.title, t.lines, t.color) if t else None,
                counts=(ok("mail", snap.mail_new), ok("approval", snap.approval_wait), ok("todoist", left)))
            return out, wide

    # ---------- 입력 ----------
    def show_toast(self, title: str, lines, color: str = render.PURPLE, sec: float | None = None) -> None:
        self.toast = Toast(title, tuple(lines), color, self.mono() + (sec or self.settings.toast_sec))

    def _async_toast(self, title: str, color: str) -> Callable[[bool, str], None]:
        """백그라운드 작업 결과를 넓은 칸에 띄우는 콜백."""

        def done(ok: bool, msg: str) -> None:
            with self.lock:
                self.show_toast(title, (msg,), color if ok else render.RED, self.settings.toast_sec * 1.5)
            if self.on_change:
                self.on_change()

        return done

    def go(self, page: int) -> None:
        self.page = page % len(PAGES)
        self.toast = None

    def press(self, idx: int) -> None:
        with self.lock:
            self.pressed[idx] = self.mono()

    def release(self, idx: int) -> str:
        with self.lock:
            held = self.mono() - self.pressed.pop(idx, self.mono())
            long = held >= self.settings.long_press_sec
            action = self._act(idx, long)
            self.actions.append(action)
            log.info("key %s %s -> %s (page %s)", idx, "long" if long else "short", action, PAGES[self.page][0])
            return action

    def tap(self, idx: int, long: bool = False) -> str:
        """누르고 떼기를 한 번에 (시뮬레이터 단축키 · 테스트)."""
        with self.lock:
            self.pressed[idx] = self.mono() - (self.settings.long_press_sec + 0.01 if long else 0)
            return self.release(idx)

    def open_setup(self) -> str:
        if self.on_setup:
            self.on_setup()
            self.show_toast("설정", ("PC 화면에 설정 창을 열었어요",), render.TEAL)
        else:
            self.show_toast("설정", ("설정 창: CharlieDeck --setup",), render.TEAL)
        return "setup"

    def open_link(self, url: str, title: str) -> str:
        if not url:
            self.show_toast(title or "열기", ("주소가 아직 없어요", "넓은 칸을 길게 누르면 설정 창"), render.ORANGE)
            return "no-link"
        color = PAGE_COLOR.get(PAGES[self.page][0], render.BLUE)
        if is_work_url(url):  # 하이웍스 · 이카운트: 로그인된 업무 브라우저로
            self.provider.open_work_url(url, self._async_toast(title or "열기", color))
            self.show_toast(title or "열기", ("업무 브라우저에서 여는 중…",), color)
            return "open-work"
        ok = self.opener(url)
        self.show_toast(title or "열기", ("Chrome 에서 열었어요" if ok else "열지 못했어요",), color if ok else render.RED)
        return "open" if ok else "open-failed"

    def _act(self, idx: int, long: bool) -> str:
        if idx == NAV_PREV:
            self.go(self.page - 1)
            return f"page:{PAGES[self.page][0]}"
        if idx == NAV_NEXT:
            self.go(self.page + 1)
            return f"page:{PAGES[self.page][0]}"
        if idx == NAV_HOME:
            if long:
                i = BRIGHTNESS_STEPS.index(self.brightness) if self.brightness in BRIGHTNESS_STEPS else 1
                self.brightness = BRIGHTNESS_STEPS[(i + 1) % len(BRIGHTNESS_STEPS)]
                if self.on_brightness:
                    self.on_brightness(self.brightness)
                self.show_toast("밝기", (f"{self.brightness}%",), render.YELLOW)
                return f"brightness:{self.brightness}"
            if self.page == 0:
                self.provider.poll(force=True)
                self.show_toast("새로고침", ("모든 연결을 다시 확인해요",), render.TEAL)
                return "refresh"
            self.go(0)
            return "page:home"
        if idx in WIDE_KEYS:
            if long:
                return self.open_setup()
            return self._upcoming()
        if not 0 <= idx < 10:
            return "ignored"
        return self._work_key(self.keys()[idx], long)

    def _upcoming(self) -> str:
        now, snap = self.clock(), self.provider.get(self.clock())
        cal = snap.state("calendar")
        if cal.status != "ok":
            self.show_toast("다음 일정", (cal.message or "일정 연결 확인", "길게 누르면 설정 창"), render.PURPLE)
            return "wide:calendar-" + cal.status
        items = snap.upcoming(now, 3)
        lines = tuple(f"{e.start:%H:%M} {e.title}" if e.start.date() == now.date()
                      else f"{e.start.month}/{e.start.day} {e.start:%H:%M} {e.title}" for e in items) or ("남은 일정 없음",)
        self.show_toast("다가오는 일정", lines, render.PURPLE, self.settings.toast_sec * 2)
        return "wide:upcoming"

    def _work_key(self, k: Key, long: bool) -> str:  # noqa: ARG002
        snap = self.provider.get(self.clock())
        if k.kind == "blank":
            return "blank"
        if k.kind == "goto":
            self.go(page_index(k.target))
            return f"page:{k.target}"
        if k.kind == "setup":
            return self.open_setup()
        if k.kind == "refresh":
            self.provider.poll(force=True)
            self.show_toast("새로고침", ("모든 연결을 다시 확인해요",), render.TEAL)
            return "refresh"
        if k.kind == "todo" and k.todo:
            if k.todo.done:
                self.provider.reopen_todo(k.todo.id, self._async_toast("할 일", render.TEAL))
                return f"todo-undo:{k.todo.id}"
            self.provider.complete_todo(k.todo.id, self._async_toast("할 일 완료", render.GREEN))
            return f"todo-done:{k.todo.id}"
        # 연결 칸: 설정이 안 됐으면 설정 창, 로그인이 필요하면 로그인 창
        src = {"mail": "mail", "approval": "approval", "calendar": "calendar", "ecount": "ecount"}.get(k.kind)
        if src:
            st = snap.state(src)
            if st.status == "off":
                return self.open_setup()
            if st.status == "login":
                self.provider.open_hiworks_login(self._async_toast("하이웍스", render.CYAN))
                self.show_toast("하이웍스", ("업무 브라우저에서 로그인하세요", "로그인하면 1분 안에 다시 읽어요"), render.YELLOW,
                                self.settings.toast_sec * 2)
                return "hiworks-login"
        hw = self.settings.section("hiworks")
        if k.kind == "mail":
            self.provider.ack_mail()
            url = hw.get("mail_url", "")
            if url:
                self.open_link(url, "메일함")
            else:
                self.show_toast("메일", ("새 메일을 확인한 것으로 표시했어요",), render.CYAN)
            return "mail"
        if k.kind == "approval":
            return "approval:" + self.open_link(hw.get("approval_url", ""), "결재함")
        if k.kind == "calendar":
            return self._upcoming()
        if k.kind == "ecount":
            self.provider.ecount_login(self._async_toast("이카운트", render.GREEN))
            self.show_toast("이카운트", ("업무 브라우저에서 접속 중…",), render.GREEN)
            return "ecount"
        if k.kind == "hiworks_login":
            self.provider.open_hiworks_login(self._async_toast("하이웍스", render.CYAN))
            self.show_toast("하이웍스", ("업무 브라우저 창을 여는 중…",), render.CYAN)
            return "hiworks-login"
        if k.kind == "mail_ack":
            self.provider.ack_mail()
            self.show_toast("메일", ("새 메일 수를 0 으로 했어요",), render.TEAL)
            return "mail-ack"
        if k.kind == "link":
            return self.open_link(k.target, k.label)
        lines = tuple(x for x in (k.detail or (k.value, k.sub)) if x)
        self.show_toast(k.label or "정보", lines or ("-",), k.color)
        return "detail"


def tile(k: Key) -> bytes:
    if k.kind == "blank":
        return _blank_tile()
    if k.kind == "todo" and k.todo:
        return render.todo_tile(k.todo.due or "오늘", k.todo.content, k.color, done=k.todo.done,
                                overdue=k.todo.overdue)
    return render.key_tile(k.label, k.value, k.sub, k.color, k.icon, alert=k.alert, dim=k.dim,
                           gauge=None if k.gauge is None else round(k.gauge, 3), wrap=k.wrap, badge=k.badge,
                           highlight=k.highlight, warn=k.warn)


_BLANK: bytes | None = None


def _blank_tile() -> bytes:
    global _BLANK
    if _BLANK is None:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (render.KEY, render.KEY), render.BG)
        ImageDraw.Draw(img).rounded_rectangle((6, 6, render.KEY - 7, render.KEY - 7), 26, fill="#121219")
        _BLANK = render.png(img)
    return _BLANK
