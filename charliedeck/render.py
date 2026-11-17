"""CharlieDeck 화면 그리기 (Pillow).

- key_tile   : 업무 버튼 196x196 PNG (머리글 + 큰 값 + 설명 + 게이지/경고)
- todo_tile  : 할 일 칸 (체크 상자 + 내용, 누르면 완료)
- nav_tile   : 10 이전 / 11 홈 / 12 다음
- wide_panel : 오른쪽 아래 넓은 칸 458x196 (날짜 · 시간 · 다음 일정, 알림 메시지)
- device_sheet : 13칸 + 넓은 칸을 D200 모양으로 한 장에 (PNG 출력·시뮬레이터용)

아이콘은 OS 마다 모양이 다른 이모지 글꼴 대신 직접 그린 도형과 assets/icons 를 쓴다
(Windows·macOS·Linux 에서 같은 그림이 나오게).
"""

from __future__ import annotations

import io
import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import fonts

KEY = 196
WIDE_W, WIDE_H = 458, 196
GAP = 22  # 기기 칸 사이 간격 (목업용)
ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons"

BG = "#16161e"
CARD = "#1f2233"
NAV_BG = "#24283b"
INK = "#e6e9f5"
SUB = "#a9b1d6"
DIM = "#565f89"
BLUE, PURPLE, GREEN, YELLOW, RED, ORANGE, CYAN, TEAL = (
    "#7aa2f7", "#bb9af7", "#9ece6a", "#e0af68", "#f7768e", "#ff9e64", "#7dcfff", "#73daca")
WEEKDAY_KO = "월화수목금토일"


def rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def mix(a: str, b: str, k: float) -> tuple[int, int, int]:
    ca, cb = rgb(a), rgb(b)
    return tuple(int(x + (y - x) * k) for x, y in zip(ca, cb))  # type: ignore[return-value]


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=6)
    return buf.getvalue()


# ---------- 글자 ----------
def fit(d: ImageDraw.ImageDraw, text: str, size: int, max_w: int, weight: str = "bold",
        min_size: int = 14) -> ImageFont.FreeTypeFont:
    """max_w 안에 들어갈 때까지 글자 크기를 줄인다."""
    while size > min_size:
        f = fonts.kr(size, weight)
        if d.textlength(text, font=f) <= max_w:
            return f
        size -= 2
    return fonts.kr(min_size, weight)


def ellipsize(d: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    if d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def wrap2(d: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    """두 줄까지 나누고 넘치면 말줄임. 가능하면 띄어쓰기에서, 아니면 글자 단위로 자른다."""
    text = text.strip()
    if d.textlength(text, font=font) <= max_w:
        return [text]
    cut = len(text)
    while cut > 1 and d.textlength(text[:cut], font=font) > max_w:
        cut -= 1
    sp = text.rfind(" ", 0, cut + 1)
    if sp > 0 and sp >= cut // 2:
        first, rest = text[:sp], text[sp + 1:]
    else:
        first, rest = text[:cut], text[cut:]
    return [first, ellipsize(d, rest.strip(), font, max_w)]


# ---------- 아이콘 ----------
@lru_cache(maxsize=64)
def _asset(name: str, size: int) -> Image.Image | None:
    p = ICONS / f"{name}.png"
    if not p.exists():
        return None
    im = Image.open(p).convert("RGBA")
    im.thumbnail((size, size), Image.LANCZOS)
    return im


ASSET_ICONS = {"calendar": "calendar", "chart": "chart", "chat": "chat", "home": "home", "sparkle": "sparkle",
               "box": "parcel"}


def draw_icon(img: Image.Image, name: str, box: tuple[int, int, int, int], color: str) -> None:
    """box 안에 아이콘 하나 (assets/icons 에 있으면 그림, 없으면 도형)."""
    x0, y0, x1, y1 = box
    size = min(x1 - x0, y1 - y0)
    if name in ASSET_ICONS:
        im = _asset(ASSET_ICONS[name], size)
        if im:
            img.paste(im, (x0 + (size - im.width) // 2, y0 + (size - im.height) // 2), im)
            return
    d = ImageDraw.Draw(img)
    c = rgb(color)
    w = max(2, size // 12)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    s = size
    if name == "mail":
        d.rounded_rectangle((x0, y0 + s * .18, x1, y1 - s * .18), s // 8, outline=c, width=w)
        d.line((x0 + w, y0 + s * .22, cx, cy + s * .05, x1 - w, y0 + s * .22), fill=c, width=w, joint="curve")
    elif name == "star":
        pts = []
        for k in range(10):
            r = s * (.48 if k % 2 == 0 else .2)
            a = -math.pi / 2 + k * math.pi / 5
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        d.polygon(pts, fill=c)
    elif name == "approve":
        d.rounded_rectangle((x0 + s * .12, y0, x1 - s * .12, y1), s // 8, outline=c, width=w)
        d.line((x0 + s * .3, cy, cx - s * .04, cy + s * .18, x1 - s * .28, cy - s * .2), fill=c, width=w + 1,
               joint="curve")
    elif name == "progress":
        d.arc((x0, y0, x1, y1), 0, 360, fill=mix(color, "#000000", .6), width=w + 1)
        d.arc((x0, y0, x1, y1), -90, 150, fill=c, width=w + 1)
    elif name == "board":
        d.rounded_rectangle((x0 + s * .08, y0 + s * .05, x1 - s * .08, y1 - s * .05), s // 8, outline=c, width=w)
        for k in range(3):
            y = y0 + s * (.3 + k * .2)
            d.line((x0 + s * .25, y, x1 - s * .25, y), fill=c, width=w)
    elif name == "clock":
        d.ellipse((x0, y0, x1, y1), outline=c, width=w)
        d.line((cx, cy, cx, y0 + s * .22), fill=c, width=w)
        d.line((cx, cy, x1 - s * .25, cy), fill=c, width=w)
    elif name == "won":
        d.ellipse((x0, y0, x1, y1), outline=c, width=w)
        f = fonts.kr(int(s * .62), "bold")
        d.text((cx, cy + 1), "₩", font=f, fill=c, anchor="mm")
    elif name == "alert":
        d.polygon(((cx, y0 + s * .04), (x1, y1 - s * .06), (x0, y1 - s * .06)), outline=c, width=w)
        d.line((cx, y0 + s * .36, cx, y0 + s * .62), fill=c, width=w + 1)
        d.ellipse((cx - w, y0 + s * .72, cx + w, y0 + s * .72 + 2 * w), fill=c)
    elif name == "truck":
        d.rectangle((x0, y0 + s * .25, x0 + s * .6, y0 + s * .7), outline=c, width=w)
        d.polygon(((x0 + s * .6, y0 + s * .4), (x1 - s * .1, y0 + s * .4), (x1, y0 + s * .55),
                   (x1, y0 + s * .7), (x0 + s * .6, y0 + s * .7)), outline=c, width=w)
        for xx in (x0 + s * .2, x0 + s * .75):
            d.ellipse((xx - s * .1, y0 + s * .68, xx + s * .1, y0 + s * .88), fill=c)
    elif name == "plus":
        d.ellipse((x0, y0, x1, y1), outline=c, width=w)
        d.line((cx, y0 + s * .25, cx, y1 - s * .25), fill=c, width=w + 1)
        d.line((x0 + s * .25, cy, x1 - s * .25, cy), fill=c, width=w + 1)
    elif name == "search":
        d.ellipse((x0, y0, x0 + s * .7, y0 + s * .7), outline=c, width=w + 1)
        d.line((x0 + s * .62, y0 + s * .62, x1, y1), fill=c, width=w + 2)
    elif name == "doc":
        d.polygon(((x0 + s * .15, y0), (x1 - s * .35, y0), (x1 - s * .15, y0 + s * .2), (x1 - s * .15, y1),
                   (x0 + s * .15, y1)), outline=c, width=w)
        for k in range(3):
            y = y0 + s * (.4 + k * .17)
            d.line((x0 + s * .3, y, x1 - s * .3, y), fill=c, width=w)
    elif name == "slides":
        d.rounded_rectangle((x0, y0 + s * .12, x1, y1 - s * .25), s // 10, outline=c, width=w)
        d.line((cx, y1 - s * .25, cx, y1 - s * .05), fill=c, width=w)
        d.polygon(((cx - s * .1, cy - s * .15), (cx - s * .1, cy + s * .08), (cx + s * .14, cy - s * .04)), fill=c)
    elif name == "sheet":
        d.rounded_rectangle((x0 + s * .05, y0 + s * .05, x1 - s * .05, y1 - s * .05), s // 10, outline=c, width=w)
        d.line((x0 + s * .05, cy - s * .1, x1 - s * .05, cy - s * .1), fill=c, width=w)
        d.line((x0 + s * .05, cy + s * .18, x1 - s * .05, cy + s * .18), fill=c, width=w)
        d.line((x0 + s * .38, y0 + s * .05, x0 + s * .38, y1 - s * .05), fill=c, width=w)
    elif name == "folder":
        d.polygon(((x0, y0 + s * .2), (x0 + s * .38, y0 + s * .2), (x0 + s * .48, y0 + s * .32), (x1, y0 + s * .32),
                   (x1, y1 - s * .12), (x0, y1 - s * .12)), outline=c, width=w)
    elif name == "browser":
        d.ellipse((x0, y0, x1, y1), outline=c, width=w)
        d.ellipse((cx - s * .17, cy - s * .17, cx + s * .17, cy + s * .17), fill=c)
        for a in (-90, 30, 150):
            r = math.radians(a)
            d.line((cx + s * .17 * math.cos(r + 2.1), cy + s * .17 * math.sin(r + 2.1),
                    cx + s * .5 * math.cos(r + 2.1), cy + s * .5 * math.sin(r + 2.1)), fill=c, width=w)
    elif name == "refresh":
        d.arc((x0 + w, y0 + w, x1 - w, y1 - w), 40, 320, fill=c, width=w + 1)
        ax, ay = cx + (s / 2 - w) * math.cos(math.radians(40)), cy + (s / 2 - w) * math.sin(math.radians(40))
        d.polygon(((ax + s * .14, ay - s * .02), (ax - s * .08, ay - s * .02), (ax + s * .04, ay + s * .18)), fill=c)
    elif name == "check":
        d.rounded_rectangle((x0 + w, y0 + w, x1 - w, y1 - w), s // 6, outline=c, width=w)
        d.line((x0 + s * .28, cy, cx - s * .04, cy + s * .2, x1 - s * .24, cy - s * .2), fill=c, width=w + 1,
               joint="curve")
    elif name == "gear":
        d.ellipse((cx - s * .32, cy - s * .32, cx + s * .32, cy + s * .32), outline=c, width=w + 1)
        d.ellipse((cx - s * .1, cy - s * .1, cx + s * .1, cy + s * .1), fill=c)
        for k in range(8):
            a = k * math.pi / 4
            d.line((cx + s * .34 * math.cos(a), cy + s * .34 * math.sin(a), cx + s * .48 * math.cos(a),
                    cy + s * .48 * math.sin(a)), fill=c, width=w + 2)
    elif name == "user":
        d.ellipse((cx - s * .2, y0 + s * .05, cx + s * .2, y0 + s * .45), outline=c, width=w)
        d.arc((x0 + s * .1, y0 + s * .52, x1 - s * .1, y1 + s * .4), 180, 360, fill=c, width=w)
    else:  # 이름 모르는 아이콘: 둥근 점
        d.ellipse((cx - s * .2, cy - s * .2, cx + s * .2, cy + s * .2), fill=c)


def _triangle(d: ImageDraw.ImageDraw, cx: float, cy: float, s: float, left: bool, fill) -> None:
    if left:
        d.polygon(((cx - s * .55, cy), (cx + s * .4, cy - s * .55), (cx + s * .4, cy + s * .55)), fill=fill)
    else:
        d.polygon(((cx + s * .55, cy), (cx - s * .4, cy - s * .55), (cx - s * .4, cy + s * .55)), fill=fill)


# ---------- 업무 버튼 ----------
@lru_cache(maxsize=512)
def key_tile(label: str, value: str, sub: str = "", color: str = BLUE, icon: str = "", *,
             alert: bool = False, dim: bool = False, gauge: float | None = None, wrap: bool = False,
             badge: str = "", highlight: bool = False, warn: bool = False) -> bytes:
    """업무 버튼 한 칸.

    label: 위쪽 작은 이름 / value: 가운데 큰 값 / sub: 아래 설명
    alert: 빨간 테두리 / warn: 노란 테두리(로그인 필요) / dim: 설정 전·값 없음 / gauge: 0~1 아래 막대 / wrap: 값이 글자면 두 줄까지
    badge: 오른쪽 위 작은 표시 (예: '진행 중') / highlight: 카드 색을 진하게 (오늘·현재 항목)
    """
    accent = DIM if dim else color
    img = Image.new("RGB", (KEY, KEY), BG)
    d = ImageDraw.Draw(img)
    top = mix(accent, BG, .55 if highlight else .78)
    for y in range(6, KEY - 6):  # 위는 은은한 색, 아래로 갈수록 어둡게
        k = (y - 6) / (KEY - 12)
        d.line((6, y, KEY - 7, y), fill=tuple(int(a + (b - a) * k) for a, b in zip(top, rgb(CARD))))
    mask = Image.new("L", (KEY, KEY), 0)
    ImageDraw.Draw(mask).rounded_rectangle((6, 6, KEY - 7, KEY - 7), 26, fill=255)
    base = Image.new("RGB", (KEY, KEY), BG)
    base.paste(img, (0, 0), mask)
    img, d = base, ImageDraw.Draw(base)
    border = RED if alert else YELLOW if warn else mix(accent, BG, .35 if highlight else .6)
    d.rounded_rectangle((6, 6, KEY - 7, KEY - 7), 26, outline=border, width=4 if alert or warn else 2)

    # 머리글: 아이콘 + 이름
    x = 22
    if icon:
        draw_icon(img, icon, (20, 20, 50, 50), accent)
        x = 58
    bw = 0.0
    if badge:  # 배지는 이름 아래 줄 오른쪽에 (이름과 겹치지 않게)
        bf = fonts.kr(14, "bold")
        bw = d.textlength(badge, font=bf) + 14
        d.rounded_rectangle((KEY - 20 - bw, 54, KEY - 20, 74), 10, fill=accent)
        d.text((KEY - 20 - bw / 2, 64), badge, font=bf, fill=BG, anchor="mm")
    lf = fit(d, label, 21, KEY - x - 22, "bold", 13)
    d.text((x, 35), ellipsize(d, label, lf, KEY - x - 22), font=lf, fill=accent if not dim else SUB, anchor="lm")

    # 큰 값
    vcol = DIM if dim else INK
    max_w = KEY - 30
    if wrap:
        f = fonts.kr(26, "bold")
        lines = wrap2(d, value, f, max_w) if value else [""]
        shift = 6 if badge else 0
        if len(lines) == 1:
            f = fit(d, value, 34, max_w, "bold", 24)
            d.text((KEY / 2, 100 + shift), value, font=f, fill=vcol, anchor="mm")
        else:
            for k, line in enumerate(lines):
                d.text((KEY / 2, 88 + shift + k * 30), line, font=f, fill=vcol, anchor="mm")
    else:
        f = fit(d, value, 64 if len(value) <= 4 else 46, max_w, "bold", 22)
        d.text((KEY / 2, 100), value, font=f, fill=vcol, anchor="mm")

    # 설명
    if sub:
        sf = fit(d, sub, 19, KEY - 28, "regular", 13)
        d.text((KEY / 2, 148 if gauge is None else 140), ellipsize(d, sub, sf, KEY - 28), font=sf, fill=SUB,
               anchor="mm")
    if gauge is not None:
        g = max(0.0, min(1.0, gauge))
        d.rounded_rectangle((22, 160, KEY - 22, 172), 6, fill="#3a3a4a")
        if g > 0:
            d.rounded_rectangle((22, 160, 22 + (KEY - 44) * g, 172), 6, fill=accent)
    if alert:
        d.ellipse((KEY - 34, 12, KEY - 18, 28), fill=RED)
    return png(img)


@lru_cache(maxsize=256)
def todo_tile(due: str, content: str, color: str = BLUE, *, done: bool = False, overdue: bool = False) -> bytes:
    """할 일 한 칸: 위 = 체크 상자 + 마감, 가운데 = 할 일 내용(세 줄까지). done 이면 초록 체크 + 취소선."""
    img = Image.new("RGB", (KEY, KEY), BG)
    d = ImageDraw.Draw(img)
    fill = mix(GREEN, BG, .72) if done else mix(color, CARD, .82)
    d.rounded_rectangle((6, 6, KEY - 7, KEY - 7), 26, fill=fill,
                        outline=GREEN if done else RED if overdue else mix(color, BG, .45), width=4 if overdue or done else 2)
    box = (20, 20, 50, 50)
    if done:
        d.rounded_rectangle(box, 8, fill=GREEN)
        d.line((27, 35, 33, 42, 44, 27), fill=rgb(BG), width=5, joint="curve")
    else:
        d.rounded_rectangle(box, 8, outline=color, width=3)
    df = fonts.kr(18, "bold")
    label = "완료!" if done else due
    d.text((60, 35), ellipsize(d, label, df, KEY - 80), font=df, fill=GREEN if done else RED if overdue else SUB,
           anchor="lm")
    f = fonts.kr(24, "bold")
    lines: list[str] = []
    rest = content.strip()
    for _ in range(3):  # 세 줄까지 (넘치면 말줄임)
        if not rest:
            break
        if d.textlength(rest, font=f) <= KEY - 34 or len(lines) == 2:
            lines.append(ellipsize(d, rest, f, KEY - 34))
            break
        cut = len(rest)
        while cut > 1 and d.textlength(rest[:cut], font=f) > KEY - 34:
            cut -= 1
        sp = rest.rfind(" ", 0, cut + 1)
        cut = sp if sp > cut // 2 else cut
        lines.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    top = 112 - (len(lines) - 1) * 16
    col = SUB if done else INK
    for k, line in enumerate(lines):
        y = top + k * 32
        d.text((KEY / 2, y), line, font=f, fill=col, anchor="mm")
        if done:
            w = d.textlength(line, font=f)
            d.line((KEY / 2 - w / 2, y, KEY / 2 + w / 2, y), fill=col, width=2)
    hint = fonts.kr(14, "regular")
    d.text((KEY / 2, KEY - 24), "다시 누르면 취소" if done else "누르면 완료", font=hint,
           fill=GREEN if done else DIM, anchor="mm")
    return png(img)


@lru_cache(maxsize=64)
def nav_tile(kind: str, page_no: int, total: int, page_title: str, target_title: str = "",
             brightness: int | None = None) -> bytes:
    """kind: prev | home | next"""
    img = Image.new("RGB", (KEY, KEY), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 6, KEY - 7, KEY - 7), 26, fill=NAV_BG, outline="#2f3549", width=2)
    small = fonts.kr(17, "regular")
    if kind in ("prev", "next"):
        d.text((KEY / 2, 32), "이전" if kind == "prev" else "다음", font=fonts.kr(20, "bold"), fill=SUB, anchor="mm")
        _triangle(d, KEY / 2, 96, 52, kind == "prev", rgb(INK))
        if target_title:
            f = fit(d, target_title, 20, KEY - 30, "regular", 13)
            d.text((KEY / 2, 156), target_title, font=f, fill=SUB, anchor="mm")
    else:
        d.text((KEY / 2, 32), f"{page_no}/{total}", font=fonts.kr(20, "bold"), fill=SUB, anchor="mm")
        draw_icon(img, "home", (KEY // 2 - 30, 56, KEY // 2 + 30, 116), INK)
        f = fit(d, page_title, 22, KEY - 26, "bold", 13)
        d.text((KEY / 2, 140), page_title, font=f, fill=INK, anchor="mm")
        if brightness is not None:
            d.text((KEY / 2, 168), f"길게: 밝기 {brightness}%", font=small, fill=DIM, anchor="mm")
    return png(img)


# ---------- 넓은 칸 ----------
def _event_lines(ev, now: datetime) -> tuple[str, str]:
    """(상태 칩 문구, 칩 색)"""
    st = ev.state(now)
    if st == "now":
        return "진행 중", GREEN
    if ev.start.date() != now.date():
        return ("내일" if (ev.start.date() - now.date()).days == 1 else f"{ev.start.month}/{ev.start.day}"), DIM
    mins = int((ev.start - now).total_seconds() // 60)
    if st == "soon":
        return f"{max(1, mins)}분 후", YELLOW
    h, m = divmod(mins, 60)
    return (f"{h}시간 {m}분 후" if m else f"{h}시간 후") if h else f"{mins}분 후", BLUE


def wide_panel(now: datetime, page_titles: tuple[str, ...], page_idx: int, next_event=None, *,
               calendar: tuple[str, str] = ("ok", ""), demo: bool = False,
               toast: tuple[str, tuple[str, ...], str] | None = None,
               counts: tuple[int | None, int | None, int | None] | None = None) -> bytes:
    """넓은 칸: 왼쪽 시간·날짜·숫자·페이지 점, 오른쪽 다음 일정 (또는 잠깐 뜨는 알림).

    calendar: (일정 연결 상태, 안내 문구) — ok 가 아니면 다음 일정 대신 안내
    counts: (메일, 결재 대기, 남은 할 일) — 왼쪽 아래 작은 숫자 (None 은 표시 안 함)
    toast: (제목, 줄들, 색)
    """
    img = Image.new("RGB", (WIDE_W, WIDE_H), "#0f1017")
    d = ImageDraw.Draw(img)
    # 왼쪽: 시각
    hhmm = now.strftime("%H:%M")
    size = 80
    while size > 40 and d.textlength(hhmm, font=fonts.number(size, "semibold")) > 186:
        size -= 4
    d.text((22, 62), hhmm, font=fonts.number(size, "semibold"), fill=INK, anchor="lm")
    date = f"{now.month}월 {now.day}일 ({WEEKDAY_KO[now.weekday()]})"
    d.text((24, 118), date, font=fonts.kr(22, "bold"), fill=SUB, anchor="lm")
    if counts and any(c is not None for c in counts):
        x = 24
        for icon, val, col in (("mail", counts[0], CYAN), ("approve", counts[1], YELLOW), ("check", counts[2], RED)):
            if val is None:
                continue
            draw_icon(img, icon, (x, 141, x + 18, 159), col)
            f = fonts.kr(18, "bold")
            d.text((x + 24, 150), str(val), font=f, fill=INK, anchor="lm")
            x += 24 + int(d.textlength(str(val), font=f)) + 18
    # 페이지 점
    n = len(page_titles)
    for k in range(n):
        cx = 30 + k * 18
        if k == page_idx:
            d.rounded_rectangle((cx - 8, 172, cx + 8, 180), 4, fill=BLUE)
        else:
            d.ellipse((cx - 4, 172, cx + 4, 180), fill="#3b4261")
    if demo:
        bf = fonts.kr(13, "bold")
        d.rounded_rectangle((30 + n * 18 + 4, 168, 30 + n * 18 + 56, 184), 8, fill=ORANGE)
        d.text((30 + n * 18 + 30, 176), "DEMO", font=bf, fill="#0f1017", anchor="mm")
    d.line((226, 22, 226, WIDE_H - 22), fill="#2a2e42", width=2)

    # 오른쪽: 알림 또는 다음 일정
    x0, x1 = 244, WIDE_W - 18
    w = x1 - x0
    if toast:
        title, lines, color = toast
        d.rounded_rectangle((x0 - 6, 18, x1 + 4, WIDE_H - 18), 16, outline=color, width=2)
        d.text((x0 + 8, 46), ellipsize(d, title, fonts.kr(24, "bold"), w - 20), font=fonts.kr(24, "bold"),
               fill=color, anchor="lm")
        for k, line in enumerate(lines[:3]):
            f = fonts.kr(18, "regular")
            d.text((x0 + 8, 88 + k * 30), ellipsize(d, line, f, w - 20), font=f, fill=INK, anchor="lm")
        return png(img)

    d.text((x0, 34), "다음 일정", font=fonts.kr(18, "bold"), fill=PURPLE, anchor="lm")
    status, message = calendar
    if status != "ok":
        title = {"off": "일정 설정 필요", "loading": "일정 확인 중…"}.get(status, "일정을 못 가져왔어요")
        d.text((x0, 86), title, font=fonts.kr(24, "bold"), fill=DIM if status != "error" else RED, anchor="lm")
        hint = message if status == "error" else "넓은 칸을 길게 누르면 설정 창" if status == "off" else ""
        if hint:
            d.text((x0, 122), ellipsize(d, hint, fonts.kr(16, "regular"), w), font=fonts.kr(16, "regular"), fill=DIM,
                   anchor="lm")
        return png(img)
    if next_event is None:
        d.text((x0, 86), "남은 일정 없음", font=fonts.kr(24, "bold"), fill=SUB, anchor="lm")
        d.text((x0, 122), "오늘 일정이 모두 끝났어요", font=fonts.kr(16, "regular"), fill=DIM, anchor="lm")
        return png(img)
    chip, chip_col = _event_lines(next_event, now)
    cf = fonts.kr(14, "bold")
    cw = d.textlength(chip, font=cf) + 16
    d.rounded_rectangle((x1 - cw, 23, x1, 45), 11, fill=chip_col)
    d.text((x1 - cw / 2, 34), chip, font=cf, fill="#0f1017", anchor="mm")
    span = f"{next_event.start:%H:%M}–{next_event.end:%H:%M}"
    d.text((x0, 72), span, font=fonts.number(34, "semibold"), fill=INK, anchor="lm")
    tf = fonts.kr(22, "bold")
    lines = wrap2(d, next_event.title, tf, w)
    for k, line in enumerate(lines):
        d.text((x0, 112 + k * 28), line, font=tf, fill=INK, anchor="lm")
    if next_event.place:
        pf = fonts.kr(16, "regular")
        y = 112 + len(lines) * 28 + 4
        d.text((x0, min(y, WIDE_H - 22)), ellipsize(d, next_event.place, pf, w), font=pf, fill=SUB, anchor="lm")
    return png(img)


# ---------- 기기 모양 한 장 (PNG 출력 · 시뮬레이터) ----------
def _rounded(im: Image.Image, r: int) -> Image.Image:
    im = im.convert("RGBA")
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), r, fill=255)
    im.putalpha(mask)
    return im


def key_origin(idx: int, pad: int = 40, top: int = 0) -> tuple[int, int]:
    return pad + (idx % 5) * (KEY + GAP), top + pad + (idx // 5) * (KEY + GAP)


def wide_origin(pad: int = 40, top: int = 0) -> tuple[int, int]:
    return pad + 3 * (KEY + GAP), top + pad + 2 * (KEY + GAP)


def device_sheet(keys: dict[int, bytes], wide: bytes, title: str = "", footer: str = "") -> Image.Image:
    """13칸 + 넓은 칸을 D200 배치대로 한 장에 그린다 (버튼 번호 표시 포함)."""
    pad = 40
    top = 54 if title else 0
    bottom = 34 if footer else 0
    W = 2 * pad + 5 * KEY + 4 * GAP
    H = top + 2 * pad + 3 * KEY + 2 * GAP + bottom
    img = Image.new("RGBA", (W, H), (10, 10, 14, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, top + 8, W - 9, H - bottom - 9), 44, fill=(24, 25, 32), outline=(58, 60, 76), width=3)
    if title:
        d.text((pad, 28), title, font=fonts.kr(24, "bold"), fill=rgb(INK), anchor="lm")
    num = fonts.kr(13, "bold")
    for i in range(13):
        x, y = key_origin(i, pad, top)
        if i in keys:
            img.alpha_composite(_rounded(Image.open(io.BytesIO(keys[i])).resize((KEY, KEY)), 24), (x, y))
        else:
            d.rounded_rectangle((x, y, x + KEY - 1, y + KEY - 1), 24, fill=(18, 18, 24))
        d.text((x + 6, y + KEY + 2), str(i), font=num, fill=(90, 96, 128))
    wx, wy = wide_origin(pad, top)
    img.alpha_composite(_rounded(Image.open(io.BytesIO(wide)).resize((2 * KEY + GAP, KEY)), 24), (wx, wy))
    d.text((wx + 6, wy + KEY + 2), "13·14 (넓은 칸)", font=num, fill=(90, 96, 128))
    if footer:
        d.text((pad, H - bottom / 2 - 4), footer, font=fonts.kr(15, "regular"), fill=rgb(DIM), anchor="lm")
    return img.convert("RGB")
