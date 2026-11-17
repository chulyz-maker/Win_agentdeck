"""화면을 보내는 곳 (백엔드). 모두 같은 모양이다.

    show(keys: {0..12: PNG}, wide: PNG)   화면 한 장 보내기
    set_brightness(percent)
    on_press / on_release                 버튼 콜백 (번호 하나를 받는다)
    start() / close()
    disconnected                          threading.Event (장치가 끊기면 set)

- D200Backend     : 실제 Ulanzi D200 (hidapi). Windows 기본 HID 드라이버로 동작, 별도 드라이버 불필요.
- HeadlessBackend : 화면 없음. 마지막 프레임만 기억 (테스트·CI·--headless).
- SimulatorBackend: Tkinter 창에 D200 모양으로 그리고 마우스로 누른다 (D200 없이 demo).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

log = logging.getLogger("charliedeck")


class Backend:
    name = "base"

    def __init__(self) -> None:
        self.on_press: Callable[[int], None] | None = None
        self.on_release: Callable[[int], None] | None = None
        self.disconnected = threading.Event()

    def start(self) -> None: ...
    def show(self, keys: dict[int, bytes], wide: bytes) -> None: ...
    def set_brightness(self, percent: int) -> None: ...
    def close(self) -> None: ...


class HeadlessBackend(Backend):
    name = "headless"

    def __init__(self) -> None:
        super().__init__()
        self.frames = 0
        self.last: tuple[dict[int, bytes], bytes] | None = None
        self.brightness: int | None = None

    def show(self, keys: dict[int, bytes], wide: bytes) -> None:
        self.frames += 1
        self.last = (dict(keys), wide)

    def set_brightness(self, percent: int) -> None:
        self.brightness = percent


class D200Backend(Backend):
    """d200.D200 을 감싼다. 버튼 이미지는 미리 그린 PNG 를 그대로 보낸다 (Button.image)."""

    name = "d200"

    def __init__(self) -> None:
        super().__init__()
        from d200 import D200  # hidapi 가 없으면 여기서 RuntimeError (안내 문구 포함)

        self.deck = D200()
        self.deck.on_event = self._event
        self.disconnected = self.deck.disconnected

    def _event(self, ev) -> None:
        if ev.kind != "button":
            return
        if ev.action == "press" and self.on_press:
            self.on_press(ev.index)
        elif ev.action == "release" and self.on_release:
            self.on_release(ev.index)

    def start(self) -> None:
        from d200 import SMALL_BACKGROUND

        self.deck.start()
        self.deck.set_small_window(mode=SMALL_BACKGROUND)  # 넓은 칸 = 우리가 그린 이미지

    def show(self, keys: dict[int, bytes], wide: bytes) -> None:
        from d200 import Button

        self.deck.set_buttons({i: Button(image=p) for i, p in keys.items()}, small_png=wide)

    def set_brightness(self, percent: int) -> None:
        self.deck.set_brightness(percent)

    def close(self) -> None:
        self.deck.close()


class SimulatorBackend(Backend):
    """Tkinter D200 시뮬레이터. Tk 는 메인 스레드에서만 돌아야 하므로 run_mainloop() 를 메인에서 부른다.

    마우스: 칸을 누르고 떼기 (0.8초 이상 누르면 길게)
    키보드: ← 이전 / → 다음 / Home·H 홈 / 1~9,0 업무 버튼 / S 설정 창 / Esc 종료
    """

    name = "simulator"
    KEYMAP = {"Left": 10, "Right": 12, "Home": 11, "h": 11, "H": 11,
              **{str(n): (n - 1) % 10 for n in range(10)}}  # 1→0 … 9→8, 0→9

    def __init__(self, scale: float = 0.75, title: str = "CharlieDeck 시뮬레이터") -> None:
        super().__init__()
        import tkinter as tk  # noqa: PLC0415  (Windows 용 python.org 설치본에 기본 포함)

        from PIL import ImageTk  # noqa: PLC0415

        from .render import GAP, KEY, device_sheet

        self.tk, self.ImageTk, self._sheet = tk, ImageTk, device_sheet
        self.scale = scale
        self.KEY, self.GAP = KEY, GAP
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg="#0a0a0e")
        self.label = tk.Label(self.root, bg="#0a0a0e", bd=0, highlightthickness=0)
        self.label.pack()
        bar = tk.Frame(self.root, bg="#0a0a0e")
        bar.pack(fill="x", padx=8, pady=(0, 6))
        self.status = tk.Label(bar, text="", fg="#7a80a0", bg="#0a0a0e", anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.on_setup: Callable[[], None] | None = None
        tk.Button(bar, text="설정", command=lambda: self.on_setup and self.on_setup(), bg="#24283b", fg="#e6e9f5",
                  activebackground="#3b4261", relief="flat", padx=14).pack(side="right")
        self._q: queue.Queue = queue.Queue()
        self._photo = None
        self._down: int | None = None
        self.on_tick: Callable[[], None] | None = None
        self.label.bind("<ButtonPress-1>", self._mouse_down)
        self.label.bind("<ButtonRelease-1>", self._mouse_up)
        self.root.bind("<KeyPress>", self._key_down)
        self.root.bind("<KeyRelease>", self._key_up)
        self.root.bind("<KeyPress-s>", lambda _e: self.on_setup and self.on_setup())
        self.root.bind("<Escape>", lambda _e: self.close())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._held_keys: set[str] = set()
        self.brightness = 80

    # 좌표 → 버튼 번호
    def hit(self, x: float, y: float) -> int | None:
        x, y = x / self.scale, y / self.scale
        pad = 40
        step = self.KEY + self.GAP
        col, row = int((x - pad) // step), int((y - pad) // step)
        if not (0 <= col < 5 and 0 <= row < 3):
            return None
        if (x - pad) % step > self.KEY or (y - pad) % step > self.KEY:
            if not (row == 2 and col == 3):  # 넓은 칸 가운데 틈은 넓은 칸으로
                return None
        if row == 2 and col >= 3:
            return 13
        return row * 5 + col

    def _mouse_down(self, e) -> None:
        self._down = self.hit(e.x, e.y)
        if self._down is not None and self.on_press:
            self.on_press(self._down)

    def _mouse_up(self, e) -> None:  # noqa: ARG002
        if self._down is not None and self.on_release:
            self.on_release(self._down)
        self._down = None

    def _key_down(self, e) -> None:
        idx = self.KEYMAP.get(e.keysym)
        if idx is None or e.keysym in self._held_keys:  # 키 반복 무시
            return
        self._held_keys.add(e.keysym)
        if self.on_press:
            self.on_press(idx)

    def _key_up(self, e) -> None:
        idx = self.KEYMAP.get(e.keysym)
        self._held_keys.discard(e.keysym)
        if idx is not None and self.on_release:
            self.on_release(idx)

    # 다른 스레드에서 와도 안전하게: 큐에 넣고 Tk 스레드가 꺼내 그린다
    def show(self, keys: dict[int, bytes], wide: bytes) -> None:
        self._q.put((keys, wide))

    def set_brightness(self, percent: int) -> None:
        self.brightness = percent

    def _drain(self) -> None:
        item = None
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
        if item:
            keys, wide = item
            img = self._sheet(keys, wide, footer=f"마우스로 누르기 · 0.8초 이상 = 길게 · ← → 이전/다음 · H 홈 · "
                                                 f"1~0 업무 버튼 · S 설정 · Esc 종료   (밝기 {self.brightness}%)")
            if self.brightness < 100:  # 밝기 흉내
                from PIL import ImageEnhance  # noqa: PLC0415

                img = ImageEnhance.Brightness(img).enhance(0.45 + 0.55 * self.brightness / 100)
            if self.scale != 1.0:
                from PIL import Image  # noqa: PLC0415

                img = img.resize((int(img.width * self.scale), int(img.height * self.scale)), Image.LANCZOS)
            self._photo = self.ImageTk.PhotoImage(img)
            self.label.configure(image=self._photo)

    def set_status(self, text: str) -> None:
        self.status.after(0, lambda: self.status.configure(text=text))

    def run_mainloop(self) -> None:
        def loop() -> None:
            if self.disconnected.is_set():
                return
            if self.on_tick:
                try:
                    self.on_tick()
                except Exception as e:  # 한 번 실패해도 창은 계속
                    log.exception("tick error: %r", e)
            self._drain()
            self.root.after(100, loop)

        self.root.after(0, loop)
        self.root.mainloop()

    def close(self) -> None:
        self.disconnected.set()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass


def frame_png(keys: dict[int, bytes], wide: bytes, title: str = "", footer: str = "") -> bytes:
    from .render import device_sheet, png

    return png(device_sheet(keys, wide, title, footer))


__all__ = ["Backend", "HeadlessBackend", "D200Backend", "SimulatorBackend", "frame_png"]
