"""글꼴 찾기 (Windows 11 우선).

순서: 환경 변수 → assets/fonts 에 넣은 글꼴 → Windows(맑은 고딕 · Segoe UI) → 리눅스(개발·CI 용 나눔·Noto)
화면을 그리는 모든 코드는 이 모듈에서 글꼴을 받는다.

환경 변수 (선택)
    CHARLIEDECK_FONT        보통 굵기 한글 글꼴 파일
    CHARLIEDECK_FONT_BOLD   굵은 한글 글꼴 파일
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

ROOT = Path(__file__).resolve().parent
BUNDLED = ROOT / "assets" / "fonts"
WIN_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
# "이 사용자만" 설치한 글꼴 위치 (Windows 10/11)
WIN_USER_FONTS = (Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts" if os.environ.get("LOCALAPPDATA")
                  else WIN_FONTS)
NANUM = Path("/usr/share/fonts/truetype/nanum")
NOTO_CJK = Path("/usr/share/fonts/opentype/noto")
NOTO_CJK_KR = 1  # ttc 순서: JP, KR, SC, TC, HK

WEIGHTS = ("regular", "medium", "semibold", "bold", "light")
_BOLD = ("semibold", "bold")

# weight -> [(파일, ttc 번호)]  먼저 있는 파일을 쓴다
_KR: dict[str, list[tuple[Path, int]]] = {
    "regular": [(BUNDLED / "NotoSansKR-Regular.ttf", 0), (BUNDLED / "Pretendard-Regular.otf", 0),
                (WIN_FONTS / "malgun.ttf", 0), (WIN_USER_FONTS / "malgun.ttf", 0),
                (NANUM / "NanumGothic.ttf", 0), (NOTO_CJK / "NotoSansCJK-Regular.ttc", NOTO_CJK_KR)],
    "medium": [(BUNDLED / "NotoSansKR-Medium.ttf", 0), (BUNDLED / "Pretendard-Medium.otf", 0),
               (WIN_FONTS / "malgun.ttf", 0), (NANUM / "NanumGothic.ttf", 0),
               (NOTO_CJK / "NotoSansCJK-Medium.ttc", NOTO_CJK_KR)],
    "semibold": [(BUNDLED / "NotoSansKR-SemiBold.ttf", 0), (BUNDLED / "Pretendard-SemiBold.otf", 0),
                 (WIN_FONTS / "malgunbd.ttf", 0), (NANUM / "NanumGothicBold.ttf", 0),
                 (NOTO_CJK / "NotoSansCJK-Bold.ttc", NOTO_CJK_KR)],
    "bold": [(BUNDLED / "NotoSansKR-Bold.ttf", 0), (BUNDLED / "Pretendard-Bold.otf", 0),
             (WIN_FONTS / "malgunbd.ttf", 0), (WIN_USER_FONTS / "malgunbd.ttf", 0),
             (NANUM / "NanumGothicBold.ttf", 0), (NOTO_CJK / "NotoSansCJK-Bold.ttc", NOTO_CJK_KR)],
    "light": [(BUNDLED / "NotoSansKR-Light.ttf", 0), (BUNDLED / "Pretendard-Light.otf", 0),
              (WIN_FONTS / "malgunsl.ttf", 0), (WIN_FONTS / "malgun.ttf", 0),
              (NANUM / "NanumGothicLight.ttf", 0), (NOTO_CJK / "NotoSansCJK-Light.ttc", NOTO_CJK_KR)],
}

# 큰 숫자(시계) 글꼴: Segoe UI, 없으면 한글 글꼴
_NUMBER: dict[str, list[Path]] = {
    "regular": [WIN_FONTS / "segoeui.ttf"],
    "medium": [WIN_FONTS / "seguisb.ttf", WIN_FONTS / "segoeui.ttf"],
    "semibold": [WIN_FONTS / "seguisb.ttf", WIN_FONTS / "segoeuib.ttf"],
    "bold": [WIN_FONTS / "segoeuib.ttf", WIN_FONTS / "seguisb.ttf"],
    "light": [WIN_FONTS / "segoeuil.ttf", WIN_FONTS / "segoeui.ttf"],
}


def _norm(weight: str) -> str:
    return weight if weight in WEIGHTS else "regular"


@lru_cache(maxsize=None)
def resolve(weight: str = "regular") -> tuple[str, int] | None:
    """(글꼴 파일, ttc 번호). 한글 글꼴이 하나도 없으면 None."""
    weight = _norm(weight)
    env = os.environ.get("CHARLIEDECK_FONT_BOLD" if weight in _BOLD else "CHARLIEDECK_FONT") \
        or os.environ.get("CHARLIEDECK_FONT")
    if env and Path(env).is_file():
        return env, 0
    for path, index in _KR[weight]:
        if path.is_file():
            return str(path), index
    return None


def font_path(weight: str = "regular") -> str:
    hit = resolve(weight)
    return hit[0] if hit else ""


@lru_cache(maxsize=512)
def kr(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    """한글 글꼴. 아무것도 없으면 Pillow 기본 글꼴 (한글은 네모로 보임 → --list-fonts 로 확인)."""
    hit = resolve(weight)
    if hit:
        try:
            return ImageFont.truetype(hit[0], size, index=hit[1])
        except OSError:
            pass
    return ImageFont.load_default(size)


@lru_cache(maxsize=256)
def number(size: int, weight: str = "semibold") -> ImageFont.FreeTypeFont:
    """시계·시간 숫자 글꼴 (Segoe UI)."""
    for path in _NUMBER[_norm(weight)]:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return kr(size, "bold" if weight in _BOLD else _norm(weight))


def describe() -> dict[str, str]:
    """고른 글꼴 파일 (--list-fonts)."""
    out = {w: font_path(w) or "(Pillow 기본 글꼴 — 한글이 깨질 수 있음)" for w in WEIGHTS}
    out["number"] = getattr(number(40), "path", "") or "(한글 글꼴 사용)"
    return out
