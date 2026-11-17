"""CharlieDeck 일반 설정 (주소 · 주기 · 밝기 등).

저장 위치: %APPDATA%\\CharlieDeck\\charliedeck.json   (셋업 창이 만들고 고친다)
- 계정 · 비밀번호 · 토큰 · 비공개 일정 주소는 여기 두지 않는다 → vault.py (Windows 자격 증명 관리자)
- 회사마다 다른 주소(하이웍스 메일함 · 결재함 등)는 코드에 넣지 않고 셋업 창에서 입력한다.
  코드에는 누구나 같은 공개 주소(POP3 서버, 이카운트 로그인 화면, Todoist · Google 웹)만 기본값으로 둔다.
- 인증정보처럼 보이는 키(password, token …)나 계정이 들어간 주소(user:pass@)가 있으면 읽지 않고 경고한다.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from platform_util import config_dir

log = logging.getLogger("charliedeck")

CONFIG_NAME = "charliedeck.json"
MAX_LINKS = 10

SECRET_KEY = re.compile(r"(pass(word|wd)?|secret|token|api[_-]?key|apikey|cookie|session|otp|credential|private)",
                        re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 기본값 (공개 주소만). 셋업 창에서 바꿀 수 있다.
DEFAULTS: dict = {
    "brightness": 80,
    "long_press_sec": 0.8,
    "toast_sec": 3.0,
    "chrome_path": "",
    "hiworks": {
        "mail_mode": "pop3",  # pop3 = 새 메일 수 (POP3) / web = 웹 메일 화면의 '안 읽은 메일' 숫자
        "pop3_host": "pop3s.hiworks.com",
        "pop3_port": 995,
        "mail_poll_min": 2,
        "mail_url": "",  # 하이웍스 메일함 주소 (회사마다 다름)
        "approval_url": "",  # 결재 대기함 주소 (회사마다 다름)
        "mail_pattern": "",  # 웹 방식 숫자 규칙 (비우면 기본)
        "approval_pattern": "",  # 비우면 기본 규칙 (결재 대기 12 …)
        "approval_poll_min": 5,
        "approval_headless": True,  # 업무 브라우저 숫자 읽기를 창 없이
    },
    "google": {"calendar_web": "https://calendar.google.com/calendar/r", "poll_min": 10},
    "todoist": {"filter": "today | overdue", "web_url": "https://app.todoist.com/app/today", "poll_min": 1},
    "ecount": {"login_url": "https://login.ecount.com/"},
    # 바로가기 페이지 (최대 10개). 하이웍스·이카운트 주소는 업무 브라우저(로그인된 전용 Chrome)로 연다.
    "links": [
        {"name": "Genspark", "url": "https://www.genspark.ai/"},
        {"name": "Todoist", "url": "https://app.todoist.com/app/today"},
        {"name": "Google 캘린더", "url": "https://calendar.google.com/calendar/r"},
    ],
}
WORK_DOMAINS = ("hiworks", "ecount")  # 이 글자가 들어간 주소는 업무 브라우저로 연다


@dataclass
class Settings:
    data: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    source: str = ""
    exists: bool = False  # 설정 파일이 있었는지 (처음 실행이면 셋업 창을 띄운다)
    warnings: list[str] = field(default_factory=list)

    # 자주 쓰는 값
    @property
    def brightness(self) -> int:
        return int(self.data["brightness"])

    @property
    def long_press_sec(self) -> float:
        return float(self.data["long_press_sec"])

    @property
    def toast_sec(self) -> float:
        return float(self.data["toast_sec"])

    @property
    def chrome_path(self) -> str:
        return os.path.expandvars(str(self.data.get("chrome_path") or ""))

    def section(self, name: str) -> dict:
        return self.data.get(name) or {}

    def links(self) -> list[dict]:
        return list(self.data.get("links") or [])[:MAX_LINKS]


def config_path() -> Path:
    return config_dir() / CONFIG_NAME


def safe_url(url) -> str | None:
    """링크로 쓸 수 있는 주소면 그대로, 아니면 None (빈 값 · 계정정보 포함 · http(s) 아닌 주소 거부)."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or any(c in url for c in "<> \"'"):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or "@" in parts.netloc:
        return None
    if SECRET_KEY.search(parts.query or ""):
        return None
    return url


def find_secrets(data, path: str = "") -> list[str]:
    """설정 안에서 인증정보처럼 보이는 키 · 이메일 주소 위치."""
    hits: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            here = f"{path}.{k}" if path else str(k)
            if str(k).startswith("_"):
                continue
            if SECRET_KEY.search(str(k)):
                hits.append(here)
                continue
            hits += find_secrets(v, here)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            hits += find_secrets(v, f"{path}[{i}]")
    elif isinstance(data, str) and EMAIL.search(data) and not safe_url(data):
        hits.append(path)
    return hits


def _merge(base: dict, over: dict, blocked: set[str], path: str = "") -> None:
    for k, v in over.items():
        here = f"{path}.{k}" if path else k
        if k.startswith("_") or here in blocked or k not in base:
            continue
        if isinstance(base[k], dict) and isinstance(v, dict):
            _merge(base[k], v, blocked, here)
        elif isinstance(base[k], bool):
            base[k] = bool(v)
        elif isinstance(base[k], (int, float)) and not isinstance(v, bool):
            try:
                base[k] = type(base[k])(v)
            except (TypeError, ValueError):
                pass
        elif isinstance(base[k], str) and isinstance(v, str):
            base[k] = v.strip()
        elif isinstance(base[k], list) and isinstance(v, list):
            base[k] = v


def is_work_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(d in host for d in WORK_DOMAINS)


def _clean_links(s: Settings) -> None:
    items = []
    for i, item in enumerate(s.data.get("links") or []):
        if not isinstance(item, dict):
            continue
        name, url = str(item.get("name", "")).strip()[:20], safe_url(item.get("url"))
        if not url:
            s.warnings.append(f"links[{i}]: http(s) 주소가 아니거나 계정 정보가 들어 있어 무시했어요")
            continue
        items.append({"name": name or "링크", "url": url})
    s.data["links"] = items[:MAX_LINKS]
    if s.data["hiworks"].get("mail_mode") not in ("pop3", "web"):
        s.data["hiworks"]["mail_mode"] = "pop3"
    for sec, key in (("hiworks", "mail_url"), ("hiworks", "approval_url"), ("google", "calendar_web"),
                     ("todoist", "web_url"), ("ecount", "login_url")):
        v = s.data[sec].get(key, "")
        if v and not safe_url(v):
            s.warnings.append(f"{sec}.{key}: 사용할 수 없는 주소라 비웠어요")
            s.data[sec][key] = ""


def from_dict(raw: dict, source: str = "", exists: bool = True) -> Settings:
    s = Settings(source=source, exists=exists)
    secrets = find_secrets(raw)
    if secrets:
        s.warnings.append("인증정보·이메일로 보이는 항목은 설정 파일에서 읽지 않아요 (셋업 창에서 입력): " + ", ".join(secrets))
    _merge(s.data, raw, set(secrets))
    s.data["brightness"] = max(0, min(100, int(s.data["brightness"])))
    s.data["long_press_sec"] = max(0.3, min(3.0, float(s.data["long_press_sec"])))
    _clean_links(s)
    return s


def load(path: str | Path | None = None) -> Settings:
    p = Path(path) if path else config_path()
    if not p.is_file():
        return Settings(source=str(p), exists=False)
    try:
        with open(p, encoding="utf-8") as fp:
            raw = json.load(fp)
    except (OSError, ValueError) as e:
        s = Settings(source=str(p), exists=True)
        s.warnings.append(f"설정 파일을 읽지 못했어요 ({e}) — 기본값을 씁니다")
        return s
    return from_dict(raw if isinstance(raw, dict) else {}, str(p))


def save(data: dict, path: str | Path | None = None) -> Path:
    """셋업 창 저장. 인증정보처럼 보이는 키는 쓰지 않는다."""
    s = from_dict(data, exists=True)
    p = Path(path) if path else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s.data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def mtime(path: str | Path | None = None) -> float:
    try:
        return (Path(path) if path else config_path()).stat().st_mtime
    except OSError:
        return 0.0


def parse_links(text: str) -> list[dict]:
    """셋업 창 링크 칸: 한 줄에 '이름 | 주소'."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, url = line.rpartition("|") if "|" in line else ("", "", line)
        out.append({"name": name.strip() or "링크", "url": url.strip()})
    return out


def format_links(items: list[dict]) -> str:
    return "\n".join(f"{i.get('name', '')} | {i.get('url', '')}" for i in items)
