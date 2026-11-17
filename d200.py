"""Ulanzi D200 HID 드라이버 (Windows 11, 공식 앱 불필요).

프로토콜 출처: redphx/strmdck (MIT), jcalado/companion-surface-d200 (USBPcap 분석).
agentdeck (MIT, (c) 2026 jakeparkcolde) 의 드라이버를 Windows 용으로 정리한 것.
패킷: [7c 7c][cmd:2 BE][length:4 LE][data → 1024 바이트 패딩]

Windows 기본 HID 드라이버로 동작한다 (Zadig 등으로 드라이버를 바꾸지 말 것).
`hid`(hidapi) 는 장치를 열 때만 불러오므로, 장치·hidapi 없이도 build_zip · render_icon 은 쓸 수 있다.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import string
import threading
import time
import zipfile
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageDraw

import fonts

VID, PID = 0x2207, 0x0019
PACKET = 1024
HEADER = 8
COLS, ROWS = 5, 3
BUTTON_COUNT = 13  # 5 + 5 + 3, 마지막 줄 3_2~4_2 는 넓은 칸(458x196)
ICON = 196

CMD_SET_BUTTONS = 0x0001
CMD_SMALL_WINDOW = 0x0006
CMD_BRIGHTNESS = 0x000A
CMD_LABEL_STYLE = 0x000B
CMD_PARTIAL_UPDATE = 0x000D
SMALL_BACKGROUND = 2  # 넓은 칸 모드: 배경 이미지
IN_BUTTON = 0x0101
IN_DEVICE_INFO = 0x0303
VENDOR_USAGE_PAGE_MIN = 0xFF00  # 벤더 정의 HID 컬렉션


@dataclass
class InputEvent:
    index: int
    kind: str  # "button" | "encoder"
    action: str  # "press" | "release" | "left" | "right"
    state: int


@dataclass
class Button:
    """버튼 하나. image(196x196 PNG) 가 있으면 그대로 보내고, 없으면 title 을 간단히 그린다."""

    image: bytes | None = None
    title: str = ""
    bg: str = "#1e1e2e"
    fg: str = "#ffffff"


def render_icon(btn: Button) -> bytes:
    if btn.image is not None:
        return btn.image
    img = Image.new("RGB", (ICON, ICON), btn.bg)
    d = ImageDraw.Draw(img)
    d.text((ICON / 2, ICON / 2), btn.title, font=fonts.kr(52, "bold"), fill=btn.fg, anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _frame(cmd: int, length: int, data: bytes) -> bytes:
    if len(data) > PACKET - HEADER:
        raise ValueError("data too large for one frame")
    head = b"\x7c\x7c" + cmd.to_bytes(2, "big") + length.to_bytes(4, "little")
    return (head + data).ljust(PACKET, b"\x00")


def _payload_safe(payload: bytes) -> bool:
    # 펌웨어 버그: 1016 + 1024*N 위치 바이트가 0x00/0x7c 이면 안 됨
    return all(payload[i] not in (0x00, 0x7C) for i in range(PACKET - HEADER, len(payload), PACKET))


def build_zip(buttons: dict[int, Button], small_png: bytes | None = None) -> bytes:
    """manifest.json + 아이콘 PNG 를 ZIP 으로 묶는다 (펌웨어 버그 회피 포함).

    small_png: 오른쪽 아래 넓은 칸(3_2, 458x196) 배경 이미지.
    """
    icons: dict[str, bytes] = {}
    manifest: dict[str, dict] = {}
    for idx, btn in buttons.items():
        col, row = idx % COLS, idx // COLS
        png = render_icon(btn)
        # 장치가 파일명으로 아이콘을 캐시하므로 내용 해시를 이름에 넣어 바뀐 아이콘만 새로 그리게 한다
        name = f"Images/{hashlib.sha1(png).hexdigest()[:16]}.png"
        icons[name] = png
        manifest[f"{col}_{row}"] = {"State": 0, "ViewParam": [{"Icon": name, "Text": ""}]}
    if small_png:
        name = f"Images/{hashlib.sha1(small_png).hexdigest()[:16]}.png"
        icons[name] = small_png
        manifest["3_2"] = {"State": 0, "SmallViewMode": SMALL_BACKGROUND, "ViewParam": [{"Icon": name, "Text": ""}]}

    dummy = ""
    for _ in range(200):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            if dummy:
                z.writestr("dummy.txt", dummy)
            z.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
            for name, data in icons.items():
                z.writestr(name, data)
        payload = buf.getvalue()
        if _payload_safe(payload):
            return payload
        dummy += "".join(random.choices(string.ascii_letters, k=8))
    raise RuntimeError("could not build firmware-safe zip")


def _hid():
    """hidapi 를 필요할 때만 불러온다 (pip install hidapi)."""
    try:
        import hid  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("hidapi 가 설치되지 않았어요: pip install hidapi  (장치 없이 보려면 --demo --sim)") from e
    return hid


def pick_interface(infos: list[dict]) -> dict | None:
    """D200 제어용 HID 인터페이스를 고른다.

    Windows 에서는 같은 장치가 &mi_00 / &col01 처럼 여러 경로로 보이고 interface_number 가 -1 일 수 있다.
    → interface 0 + 벤더 usage page, interface 0, 벤더 usage page(0xFF00 이상), 첫 번째 순으로 고른다.
    키보드·마우스 컬렉션은 Windows 가 독점하므로 열면 Access denied 가 난다.
    """
    if not infos:
        return None
    for d in infos:
        if d.get("interface_number") == 0 and d.get("usage_page", 0) >= VENDOR_USAGE_PAGE_MIN:
            return d
    for d in infos:
        if d.get("interface_number") == 0:
            return d
    for d in infos:
        if d.get("usage_page", 0) >= VENDOR_USAGE_PAGE_MIN:
            return d
    return infos[0]


def list_devices() -> list[dict]:
    """연결된 D200 HID 경로 목록 (진단용: --list-devices)."""
    hid = _hid()
    return [{"path": d["path"].decode("utf-8", "replace") if isinstance(d["path"], bytes) else str(d["path"]),
             "interface_number": d.get("interface_number"), "usage_page": hex(d.get("usage_page", 0)),
             "usage": hex(d.get("usage", 0)), "product": d.get("product_string")} for d in hid.enumerate(VID, PID)]


class D200:
    def __init__(self) -> None:
        hid = _hid()
        info = pick_interface(hid.enumerate(VID, PID))
        if not info:
            raise RuntimeError("D200 을 찾지 못했어요 (USB 연결과 Ulanzi Studio 종료를 확인하세요)")
        self.dev = hid.device()
        try:
            self.dev.open_path(info["path"])
        except OSError as e:
            raise RuntimeError(f"D200 을 열지 못했어요 ({e}). Ulanzi Studio 등 다른 프로그램이 쓰고 있는지 확인하세요") from e
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.disconnected = threading.Event()
        self._small = "1|0|0|00:00:00|0"
        self.on_event: Callable[[InputEvent], None] | None = None
        self.on_info: Callable[[str], None] | None = None

    # ---- 송신 ----
    def _write(self, packets: list[bytes]) -> None:
        with self._lock:
            for p in packets:
                self.dev.write(b"\x00" + p)  # report ID 0 (Windows 는 앞 0바이트가 반드시 필요)

    def send(self, cmd: int, data: bytes) -> None:
        self._write([_frame(cmd, len(data), data)])

    def set_brightness(self, percent: int) -> None:
        self.send(CMD_BRIGHTNESS, str(max(0, min(100, percent))).encode())

    def set_small_window(self, mode: int = 1, cpu: int = 0, mem: int = 0, gpu: int = 0) -> None:
        """mode: 0=CPU/RAM/GPU 통계, 1=아날로그 시계, 2=배경 이미지, 200~203=디지털 시계."""
        self._small = f"{mode}|{cpu}|{mem}|{time.strftime('%H:%M:%S')}|{gpu}"
        self.send(CMD_SMALL_WINDOW, self._small.encode())

    def set_buttons(self, buttons: dict[int, Button], partial: bool = False, small_png: bytes | None = None) -> None:
        payload = build_zip(buttons, small_png)
        cmd = CMD_PARTIAL_UPDATE if partial else CMD_SET_BUTTONS
        first = payload[: PACKET - HEADER]
        packets = [_frame(cmd, len(payload), first)]
        for off in range(PACKET - HEADER, len(payload), PACKET):
            packets.append(payload[off : off + PACKET].ljust(PACKET, b"\x00"))
        self._write(packets)

    # ---- 수신 ----
    def _parse(self, r: bytes) -> None:
        if len(r) < HEADER or r[0] != 0x7C or r[1] != 0x7C:
            return
        cmd = int.from_bytes(r[2:4], "big")
        length = int.from_bytes(r[4:8], "little")
        data = r[HEADER : HEADER + length]
        if cmd == IN_BUTTON and len(data) >= 4:
            kind = "encoder" if data[2] == 0x02 else "button"
            action = {0x01: "press", 0x02: "left", 0x03: "right"}.get(data[3], "release")
            if self.on_event:
                self.on_event(InputEvent(data[1], kind, action, data[0]))
        elif cmd == IN_DEVICE_INFO and self.on_info:
            self.on_info(data.split(b"\x00")[0].decode("ascii", "replace"))

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = self.dev.read(PACKET, 100)
            except (OSError, ValueError):
                # USB 분리 등: 끊김 표시 후 종료 (상위에서 다시 연결한다)
                self.disconnected.set()
                self._stop.set()
                return
            if r:
                self._parse(bytes(r))

    def _keepalive_loop(self) -> None:
        # 앱 연결이 끊긴 걸로 판단하지 않도록 주기적으로 넓은 칸 데이터를 보낸다
        while not self._stop.wait(5):
            try:
                self.set_small_window(int(self._small.split("|")[0]))
            except (OSError, ValueError):
                self.disconnected.set()
                return

    def start(self) -> None:
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        time.sleep(0.2)
        try:
            self.dev.close()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":  # 장치 점검: python d200.py [초]
    import sys

    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    deck = D200()
    palette = ["#f7768e", "#ff9e64", "#e0af68", "#9ece6a", "#7dcfff", "#7aa2f7", "#bb9af7"]
    buttons = {i: Button(title=str(i), bg=palette[i % 7], fg="#1a1b26") for i in range(BUTTON_COUNT)}

    def handle(ev: InputEvent) -> None:
        print(f"{time.strftime('%H:%M:%S')} {ev}", flush=True)

    deck.on_event = handle
    deck.on_info = lambda s: print("DEVICE INFO:", s, flush=True)
    deck.start()
    deck.set_brightness(80)
    deck.set_buttons(buttons)
    deck.set_small_window(mode=1)
    print(f"=== {duration:.0f}초 동안 버튼 입력을 기다립니다 ===", flush=True)
    try:
        time.sleep(duration)
    finally:
        deck.close()
