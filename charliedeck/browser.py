"""업무 브라우저 — 하이웍스 · 이카운트 전용 Chrome 프로필.

왜 따로 쓰나
- 최신 Chrome 은 평소 쓰는 기본 프로필의 자동 제어를 막는다. 그래서 CharlieDeck 전용 프로필
  (%LOCALAPPDATA%\\CharlieDeck\\browser) 을 쓰고, 하이웍스 · 이카운트 로그인은 이 창에서 한다.
- 로그인 쿠키는 이 폴더에만 남는다. CharlieDeck 은 비밀번호를 이 폴더에 저장하지 않는다
  (이카운트 자동 접속은 자격 증명 관리자에서 꺼내 그 순간 입력만 한다).

동작
- open_visible(url) : 업무 브라우저 창에서 주소 열기 (이미 떠 있으면 새 탭)
- read_count(url, patterns) : 화면 글자에서 숫자 하나 읽기 (결재 대기 · 안 읽은 메일)
  창이 없으면 화면 없이(headless) 잠깐 띄웠다가 닫는다. 창이 떠 있으면 뒤쪽 탭으로 읽고 탭만 닫는다.
- ecount_login(...) : 이카운트 로그인 화면에 회사코드 · 아이디 · 비밀번호를 넣고 로그인 단추를 누른다.

자동 제어 통로(원격 디버깅 포트)는 이 PC 안(127.0.0.1)에서만 열린다. 누르는 단추는 이카운트 로그인 단추 하나뿐이다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from platform_util import data_dir, detached_flags, find_chrome, no_window_flags

log = logging.getLogger("charliedeck")

PORT = 9334
LOCK = threading.Lock()  # 같은 프로필은 한 번에 한 작업만
HIWORKS_LOGIN = "https://login.office.hiworks.com/"

LOGIN_JS = r"""(() => {
  const href = location.href.toLowerCase();
  const pw = document.querySelector('input[type=password]');
  return {href: location.href, login: /login|signin|auth/.test(href) || !!(pw && pw.offsetParent !== null),
          text: document.body ? document.body.innerText.slice(0, 20000) : ''};
})()"""

ECOUNT_FILL_JS = r"""((v) => {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  const put = (sel, val) => {
    const el = document.querySelector(sel);
    if (!el) return false;
    el.focus(); setter.call(el, val);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  };
  if (!put('#com_code', v.c) || !put('#id', v.u) || !put('#passwd', v.p)) return 'nofield';
  const btn = document.querySelector('#save');
  if (!btn) return 'nobutton';
  btn.click();
  return 'clicked';
})"""

DEFAULT_APPROVAL_PATTERNS = (
    r"결재\s*대기(?:\s*문서|\s*함)?\D{0,8}?(\d{1,4})",
    r"대기\s*문서\D{0,8}?(\d{1,4})",
    r"미결(?:\s*문서|\s*함)?\D{0,8}?(\d{1,4})",
)
DEFAULT_MAIL_PATTERNS = (
    r"안\s*읽은\s*메일\D{0,8}?(\d{1,5})",
    r"받은\s*메일함\D{0,6}?(\d{1,5})",
)


class BrowserError(RuntimeError):
    pass


@dataclass
class ReadResult:
    status: str  # ok | login | nomatch | error
    count: int | None = None
    snippet: str = ""  # 셋업 창 '테스트' 에서만 보여준다 (저장 · 기록하지 않음)
    message: str = ""


def profile_dir() -> Path:
    return data_dir() / "browser"


def compile_patterns(custom: str, defaults: tuple[str, ...]) -> list[re.Pattern]:
    """셋업 창의 규칙(한 줄에 하나, 숫자 자리는 (\\d+)) + 기본 규칙."""
    out = []
    for p in [x.strip() for x in (custom or "").splitlines() if x.strip()] + list(defaults):
        try:
            rx = re.compile(p)
        except re.error:
            continue
        if rx.groups >= 1:
            out.append(rx)
    return out


def extract_count(text: str, patterns: list[re.Pattern]) -> tuple[int | None, str]:
    """글자에서 첫 번째로 맞는 규칙의 숫자와 그 주변 글자."""
    text = re.sub(r"[ \t\u00a0]+", " ", text or "")
    for rx in patterns:
        m = rx.search(text)
        if m:
            a, b = max(0, m.start() - 20), min(len(text), m.end() + 20)
            return int(m.group(1)), text[a:b].replace("\n", " ")
    return None, ""


def hint_snippet(text: str, word: str) -> str:
    """숫자를 못 찾았을 때 셋업 창에 보여줄 단서: word 주변 글자."""
    i = (text or "").find(word)
    if i < 0:
        return ""
    return text[max(0, i - 30): i + 60].replace("\n", " ")


class _CDP:
    def __init__(self, ws) -> None:
        self.ws, self.n = ws, 0

    async def call(self, method: str, params: dict | None = None, sid: str | None = None, timeout: float = 30):
        self.n += 1
        mid = self.n
        msg = {"id": mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        await self.ws.send(json.dumps(msg))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise BrowserError(f"브라우저 응답 시간 초과 ({method})")
            r = json.loads(await asyncio.wait_for(self.ws.recv(), left))
            if r.get("id") == mid:
                if "error" in r:
                    raise BrowserError(f"{method}: {r['error'].get('message', r['error'])}")
                return r.get("result", {})

    async def evaluate(self, expr: str, sid: str, timeout: float = 20):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True},
                            sid, timeout)
        return r.get("result", {}).get("value")


class WorkBrowser:
    def __init__(self, chrome_path: str = "", port: int = PORT, profile: Path | None = None) -> None:
        self.chrome_path = chrome_path
        self.port = port
        self.profile = profile or profile_dir()

    # ---------- 실행 ----------
    def version(self) -> dict | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1.5) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def alive(self) -> bool:
        return self.version() is not None

    def _launch(self, mode: str, url: str | None = None) -> subprocess.Popen:
        """mode: visible | headless | minimized"""
        chrome = find_chrome(self.chrome_path)
        if not chrome:
            raise BrowserError("Chrome 또는 Edge 를 찾지 못했어요 (설정 → 일반 → Chrome 경로)")
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [chrome, f"--user-data-dir={self.profile}", f"--remote-debugging-port={self.port}",
                "--no-first-run", "--no-default-browser-check"]
        if mode == "headless":
            args += ["--headless=new", "--window-size=1400,1000", "--disable-gpu"]
        elif mode == "minimized":
            args += ["--start-minimized"]
        if url:
            args.append(url)
        flags = detached_flags() if mode == "visible" else no_window_flags()
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        for _ in range(60):
            if self.alive():
                return proc
            if proc.poll() is not None and not self.alive():
                break
            time.sleep(0.25)
        raise BrowserError("업무 브라우저를 띄우지 못했어요")

    async def _close_browser(self) -> None:
        import websockets  # noqa: PLC0415

        ver = self.version()
        if not ver:
            return
        try:
            async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=None, open_timeout=5) as ws:
                await _CDP(ws).call("Browser.close", timeout=5)
        except Exception:
            pass

    def _stop(self, proc: subprocess.Popen | None) -> None:
        """우리가 잠깐 띄운 브라우저 닫기 (쿠키가 저장되도록 정상 종료 먼저)."""
        if not proc:
            return
        try:
            asyncio.run(self._close_browser())
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    # ---------- 탭 ----------
    async def _page(self, url: str, work, *, foreground: bool = False, keep: bool = False, ua_fix: bool = False):
        import websockets  # noqa: PLC0415

        ver = self.version()
        if not ver:
            raise BrowserError("업무 브라우저가 꺼져 있어요")
        async with websockets.connect(ver["webSocketDebuggerUrl"], max_size=None, open_timeout=10) as ws:
            c = _CDP(ws)
            tid = (await c.call("Target.createTarget", {"url": "about:blank", "background": not foreground}))["targetId"]
            sid = (await c.call("Target.attachToTarget", {"targetId": tid, "flatten": True}))["sessionId"]
            try:
                if ua_fix:  # 화면 없는 Chrome 표시(HeadlessChrome)를 일반 Chrome 으로
                    ua = ver.get("User-Agent", "").replace("HeadlessChrome", "Chrome")
                    if ua:
                        await c.call("Network.setUserAgentOverride", {"userAgent": ua}, sid)
                await c.call("Page.enable", sid=sid)
                await c.call("Page.navigate", {"url": url}, sid)
                if foreground:
                    await c.call("Target.activateTarget", {"targetId": tid})
                return await work(c, sid)
            finally:
                if not keep:
                    try:
                        await c.call("Target.closeTarget", {"targetId": tid}, timeout=5)
                    except Exception:
                        pass

    # ---------- 공개 동작 ----------
    def open_visible(self, url: str) -> None:
        """업무 브라우저 창에서 열기. 창이 없으면 띄운다."""
        with LOCK:
            if not self.alive():
                self._launch("visible", url)
                return

            async def noop(c, sid):  # noqa: ARG001
                return None

            asyncio.run(self._page(url, noop, foreground=True, keep=True))

    def read_count(self, url: str, patterns: list[re.Pattern], mode: str = "headless", timeout: float = 25,
                   hint_word: str = "결재") -> ReadResult:
        """url 을 열어 화면 글자에서 숫자를 찾는다. mode: headless | minimized (창이 없을 때만 의미 있음)."""
        with LOCK:
            started = None
            if not self.alive():
                started = self._launch(mode)
            try:
                return asyncio.run(self._read(url, patterns, timeout, started is not None and mode == "headless",
                                              hint_word))
            finally:
                self._stop(started)

    async def _read(self, url, patterns, timeout, ua_fix, hint_word) -> ReadResult:
        async def work(c, sid):
            deadline = time.monotonic() + timeout
            last: dict = {}
            await asyncio.sleep(2)
            while time.monotonic() < deadline:
                try:
                    last = await c.evaluate(LOGIN_JS, sid) or {}
                except BrowserError:
                    last = {}
                count, snip = extract_count(last.get("text", ""), patterns)
                if count is not None:
                    return ReadResult("ok", count, snip)
                await asyncio.sleep(1)
            if last.get("login"):
                return ReadResult("login", message="하이웍스 로그인이 필요해요")
            return ReadResult("nomatch", snippet=hint_snippet(last.get("text", ""), hint_word),
                              message="화면에서 숫자를 찾지 못했어요")

        return await self._page(url, work, ua_fix=ua_fix)

    def ecount_login(self, url: str, company: str, user: str, password: str, timeout: float = 15) -> tuple[bool, str]:
        """이카운트 로그인 화면에 입력하고 로그인 단추를 누른다 (업무 브라우저 창, 새 탭)."""
        if not (company and user and password):
            return False, "이카운트 회사코드·아이디·비밀번호를 설정하세요"
        with LOCK:
            if not self.alive():
                self._launch("visible", "about:blank")
            values = json.dumps({"c": company, "u": user, "p": password})

            async def work(c, sid):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    ready = await c.evaluate("!!document.querySelector('#com_code') && "
                                             "!!document.querySelector('#passwd')", sid)
                    if ready:
                        break
                    href = await c.evaluate("location.href", sid) or ""
                    if href and "login.ecount.com" not in href and href != "about:blank":
                        return True, "이미 로그인돼 있어요"
                    await asyncio.sleep(0.5)
                else:
                    return False, "로그인 화면을 찾지 못했어요"
                r = await c.evaluate(f"{ECOUNT_FILL_JS}({values})", sid)
                if r != "clicked":
                    return False, "로그인 화면 구조가 바뀌었어요 (입력 칸·단추 없음)"
                for _ in range(int(timeout * 2)):
                    await asyncio.sleep(0.5)
                    href = await c.evaluate("location.href", sid) or ""
                    if "login.ecount.com" not in href:
                        return True, "이카운트에 접속했어요"
                return True, "로그인 단추를 눌렀어요 — 창을 확인하세요"

            return asyncio.run(self._page(url, work, foreground=True, keep=True))
