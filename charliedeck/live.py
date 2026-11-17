"""실제 연결 — API 가 없는 하이웍스 · 이카운트도 포함.

| 칸          | 방법                                                                          | 주기(기본) |
|-------------|-------------------------------------------------------------------------------|-----------|
| 메일        | 하이웍스 POP3(pop3s.hiworks.com:995) — 머리글(TOP)만 읽고 메일은 지우지 않는다    | 2분       |
|             | (선택) 웹 메일 화면에서 '안 읽은 메일' 숫자 읽기 — 업무 브라우저                  |           |
| 결재 대기   | 업무 브라우저(전용 Chrome)로 결재 대기함 화면의 숫자 읽기                        | 5분       |
| 오늘 일정   | Google 캘린더 비공개 iCal 주소                                                 | 10분      |
| 할 일       | Todoist API v1 (개인 토큰)                                                     | 1분       |
| 이카운트    | 누르면 업무 브라우저에서 자동 로그인                                           | -         |

POP3 는 '읽음' 정보를 알 수 없다 → '새 메일' = 마지막으로 메일 칸을 누른 뒤 새로 온 메일 수.
계정 · 비밀번호 · 토큰 · iCal 주소는 vault(자격 증명 관리자)에서 꺼내 쓰고, 화면 · 로그에는 남기지 않는다.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import hashlib
import json
import logging
import poplib
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Callable

from platform_util import data_dir, open_url

from .browser import (DEFAULT_APPROVAL_PATTERNS, DEFAULT_MAIL_PATTERNS, HIWORKS_LOGIN, BrowserError, WorkBrowser,
                      compile_patterns)
from .model import SOURCES, Done, Event, ProviderBase, Snapshot, SourceState, Todo
from .settings import Settings
from .vault import Vault, mask

log = logging.getLogger("charliedeck")

TODOIST_API = "https://api.todoist.com/api/v1"
HTTP_TIMEOUT = 20
RECENT_MAIL = 3


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # noqa: PLC0415

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _local(dt) -> tuple[datetime, bool]:
    """iCal 시각 → (로컬 naive datetime, 종일 여부)."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt, False
    if isinstance(dt, date):
        return datetime.combine(dt, datetime.min.time()), True
    raise ValueError(f"unknown date {dt!r}")


# ---------- Google 일정 (iCal) ----------
def parse_ical(data: bytes, start: date, end: date) -> list[Event]:
    import icalendar  # noqa: PLC0415
    import recurring_ical_events  # noqa: PLC0415

    cal = icalendar.Calendar.from_ical(data)
    out: list[Event] = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s, all_day = _local(comp.decoded("DTSTART"))
        if comp.get("DTEND") is not None:
            e, _ = _local(comp.decoded("DTEND"))
        elif comp.get("DURATION") is not None:
            e = s + comp.decoded("DURATION")
        else:
            e = s + (timedelta(days=1) if all_day else timedelta(hours=1))
        out.append(Event(s, e, str(comp.get("SUMMARY", "") or "(제목 없음)"), str(comp.get("LOCATION", "") or ""),
                         all_day))
    return sorted(out, key=lambda x: x.start)


def fetch_ical(url: str, now: datetime) -> list[Event]:
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.lower().startswith("https://"):
        raise ValueError("iCal 주소는 https:// 또는 webcal:// 로 시작해야 해요")
    req = urllib.request.Request(url, headers={"User-Agent": "CharlieDeck"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ssl_context()) as r:
        data = r.read()
    monday = now.date() - timedelta(days=now.weekday())
    return parse_ical(data, monday, now.date() + timedelta(days=8))


# ---------- Todoist ----------
def _todo_due(due: dict | None, now: datetime) -> tuple[str, bool, datetime]:
    """(화면 문구, 지연 여부, 정렬 키)"""
    if not due:
        return "", False, datetime.max
    raw = due.get("datetime") or due.get("date") or ""
    try:
        if len(raw) <= 10:
            d = date.fromisoformat(raw)
            overdue = d < now.date()
            return (f"지연 {d.month}/{d.day}" if overdue else "오늘" if d == now.date() else f"{d.month}/{d.day}",
                    overdue, datetime.combine(d, datetime.max.time()))
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        overdue = dt < now
        if dt.date() == now.date():
            return (f"{dt:%H:%M}", overdue, dt)
        return (f"지연 {dt.month}/{dt.day}" if overdue else f"{dt.month}/{dt.day} {dt:%H:%M}", overdue, dt)
    except ValueError:
        return str(due.get("string", ""))[:10], False, datetime.max


def parse_todoist(items: list[dict], now: datetime) -> list[Todo]:
    rows = []
    for t in items:
        text, overdue, key = _todo_due(t.get("due"), now)
        pr = int(t.get("priority", 1) or 1)
        rows.append(((not overdue, -pr, key), Todo(str(t["id"]), str(t.get("content", "")).strip(), text, pr, overdue)))
    return [r[1] for r in sorted(rows, key=lambda r: r[0])]


class TodoistClient:
    def __init__(self, token: str, opener: Callable | None = None) -> None:
        self.token = token
        self.opener = opener or (lambda req: urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ssl_context()))

    def _req(self, method: str, path: str, params: dict | None = None):
        url = f"{TODOIST_API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {self.token}", "User-Agent": "CharlieDeck"})
        with self.opener(req) as r:
            body = r.read()
        return json.loads(body) if body else None

    def tasks(self, query: str) -> list[dict]:
        out, cursor = [], None
        for _ in range(4):  # 최대 200개
            params = {"query": query, "limit": 50}
            if cursor:
                params["cursor"] = cursor
            data = self._req("GET", "/tasks/filter", params) or {}
            out += data.get("results", [])
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return out

    def close(self, task_id: str) -> None:
        self._req("POST", f"/tasks/{urllib.parse.quote(task_id)}/close")


# ---------- 하이웍스 POP3 ----------
def _decode(value: str | None) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def _sender(value: str) -> str:
    name, addr = email.utils.parseaddr(value)
    return (name or addr.split("@")[0] or "").strip()[:20]


class Pop3Mail:
    """새 메일 수 = 기준 목록(마지막 확인)에 없는 메일 수. UIDL 은 해시로만 저장한다."""

    def __init__(self, host: str, port: int, user: str, password: str, seen_path=None,
                 factory: Callable | None = None) -> None:
        self.host, self.port, self.user, self.password = host, int(port), user, password
        self.seen_path = seen_path or data_dir() / "mail_seen.json"
        self.factory = factory or (lambda: poplib.POP3_SSL(self.host, self.port, timeout=HTTP_TIMEOUT,
                                                           context=ssl_context()))
        self.current: set[str] = set()

    def _baseline(self) -> set[str] | None:
        try:
            with open(self.seen_path, encoding="utf-8") as fp:
                data = json.load(fp)
            if data.get("account") != self._acct():
                return None
            return set(data.get("seen", []))
        except (OSError, ValueError):
            return None

    def _acct(self) -> str:
        return hashlib.sha256(f"{self.host}|{self.user}".encode()).hexdigest()[:16]

    def save_baseline(self, uids: set[str]) -> None:
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.seen_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"account": self._acct(), "seen": sorted(uids)}), encoding="utf-8")
        tmp.replace(self.seen_path)

    def check(self) -> tuple[int, list[tuple[str, str]]]:
        box = self.factory()
        try:
            box.user(self.user)
            box.pass_(self.password)
            _, lines, _ = box.uidl()
            pairs = []
            for ln in lines:
                num, _, uid = ln.decode("utf-8", "replace").partition(" ")
                pairs.append((int(num), hashlib.sha1(uid.strip().encode()).hexdigest()[:20]))
            self.current = {u for _, u in pairs}
            base = self._baseline()
            if base is None:  # 처음: 지금 있는 메일은 모두 확인한 것으로
                self.save_baseline(self.current)
                base = set(self.current)
            new = [(n, u) for n, u in pairs if u not in base]
            recent: list[tuple[str, str]] = []
            for n, _ in sorted(new, reverse=True)[:RECENT_MAIL]:  # 최근 메일 머리글만 (본문은 받지 않음)
                try:
                    _, hdr, _ = box.top(n, 0)
                    msg = email.message_from_bytes(b"\r\n".join(hdr), policy=email.policy.default)
                    recent.append((_sender(_decode(msg.get("From"))), _decode(msg.get("Subject")) or "(제목 없음)"))
                except (poplib.error_proto, ValueError):
                    continue
            return len(new), recent
        finally:
            try:
                box.quit()
            except Exception:
                pass

    def ack(self) -> None:
        if self.current:
            self.save_baseline(self.current)


# ---------- 공급자 ----------
class LiveProvider(ProviderBase):
    name = "live"

    def __init__(self, settings: Settings, vault: Vault, browser: WorkBrowser | None = None, *,
                 clock: Callable[[], datetime] | None = None, monotonic: Callable[[], float] | None = None,
                 start: bool = True, todoist: TodoistClient | None = None, pop3: Pop3Mail | None = None) -> None:
        super().__init__(monotonic)
        self.settings, self.vault = settings, vault
        self.clock = clock or datetime.now
        self.browser = browser or WorkBrowser(settings.chrome_path)
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="charliedeck")
        self.states: dict[str, SourceState] = {k: SourceState("loading", "확인 중…") for k in SOURCES}
        self.mail_new: int | None = None
        self.recent_mail: list[tuple[str, str]] = []
        self.approval_wait: int | None = None
        self.events: list[Event] = []
        self.todos: list[Todo] = []
        self.notices: list[tuple[str, tuple[str, ...], str]] = []
        self._todoist = todoist
        self._pop3 = pop3
        self._due: dict[str, float] = {k: 0.0 for k in ("mail", "approval", "calendar", "todoist")}
        self._running: set[str] = set()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._refresh_static_states()
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._loop, name="charliedeck-live", daemon=True)
            self._thread.start()

    # ---------- 설정 값 ----------
    def _sec(self, name: str) -> dict:
        return self.settings.section(name)

    def interval(self, src: str) -> float:
        key = {"mail": ("hiworks", "mail_poll_min"), "approval": ("hiworks", "approval_poll_min"),
               "calendar": ("google", "poll_min"), "todoist": ("todoist", "poll_min")}[src]
        return max(0.5, float(self._sec(key[0]).get(key[1], 5))) * 60

    def _refresh_static_states(self) -> None:
        v = self.vault
        if v.has("ecount.company") and v.has("ecount.user") and v.has("ecount.password"):
            self.states["ecount"] = SourceState("ok", "자동 접속")
        else:
            self.states["ecount"] = SourceState("off", "회사코드·아이디·비밀번호 설정")

    def _set(self, src: str, status: str, message: str = "", action: str = "setup") -> None:
        with self.lock:
            self.states[src] = SourceState(status, mask(message, self.vault.values()), action)
            self.bump()

    def notice(self, title: str, lines, color: str = "#f7768e") -> None:
        with self.lock:
            self.notices.append((title, tuple(mask(x, self.vault.values()) for x in lines), color))
            self.bump()

    def drain_notices(self) -> list[tuple[str, tuple[str, ...], str]]:
        with self.lock:
            out, self.notices = self.notices, []
            return out

    # ---------- 반복 ----------
    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self.mono()
            for src, due in list(self._due.items()):
                if now >= due and src not in self._running:
                    self._running.add(src)
                    self._due[src] = now + self.interval(src)
                    self.pool.submit(self._run, src)
            self._wake.wait(1.0)
            self._wake.clear()

    def _run(self, src: str) -> None:
        try:
            getattr(self, f"_poll_{src}")()
        except Exception as e:  # 한 연결이 실패해도 나머지는 계속
            log.warning("%s poll failed: %s", src, mask(repr(e), self.vault.values()))
            self._set(src, "error", "연결 오류 — 잠시 뒤 다시 시도")
        finally:
            self._running.discard(src)

    def poll(self, force: bool = False) -> None:
        self.expire()
        if force:
            for k in self._due:
                self._due[k] = 0.0
            self._wake.set()

    def reload(self, settings: Settings) -> None:
        """셋업 창에서 저장하면: 새 설정으로 바꾸고 모든 연결을 바로 다시 확인."""
        self.settings = settings
        self.browser.chrome_path = settings.chrome_path
        self._pop3 = None
        self._refresh_static_states()
        self.poll(force=True)

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ---------- 메일 ----------
    def _poll_mail(self) -> None:
        hw = self._sec("hiworks")
        if hw.get("mail_mode") == "web" and hw.get("mail_url"):
            r = self.browser.read_count(hw["mail_url"], compile_patterns(hw.get("mail_pattern", ""), DEFAULT_MAIL_PATTERNS),
                                        "headless" if hw.get("approval_headless", True) else "minimized", hint_word="메일")
            self._apply_read("mail", r, lambda n: setattr(self, "mail_new", n))
            return
        user, pw = self.vault.get("hiworks.user"), self.vault.get("hiworks.password")
        if not (user and pw):
            self._set("mail", "off", "메일 계정·비밀번호 설정")
            return
        box = self._pop3 or Pop3Mail(hw.get("pop3_host") or "pop3s.hiworks.com", hw.get("pop3_port") or 995, user, pw)
        self._pop3 = box
        try:
            n, recent = box.check()
        except poplib.error_proto as e:
            msg = str(e)
            self._set("mail", "error", "로그인 실패 — 계정·비밀번호 확인" if "-ERR" in msg else "메일 서버 오류")
            return
        except (OSError, socket.timeout, ssl.SSLError):
            self._set("mail", "error", "메일 서버에 연결하지 못했어요")
            return
        with self.lock:
            self.mail_new, self.recent_mail = n, recent
        self._set("mail", "ok", "POP3")

    def ack_mail(self) -> None:
        """메일 칸을 눌렀을 때: 지금까지 온 메일은 확인한 것으로 (POP3 방식)."""
        if self._pop3 and self._sec("hiworks").get("mail_mode") != "web":
            try:
                self._pop3.ack()
            except OSError:
                pass
            with self.lock:
                self.mail_new, self.recent_mail = 0, []
                self.bump()

    # ---------- 결재 ----------
    def _apply_read(self, src: str, r, setter) -> None:
        if r.status == "ok":
            with self.lock:
                setter(r.count)
            self._set(src, "ok", "업무 브라우저")
        elif r.status == "login":
            self._set(src, "login", "하이웍스 로그인 필요", "hiworks")
        else:
            self._set(src, "error", r.message or "읽지 못했어요")

    def _poll_approval(self) -> None:
        hw = self._sec("hiworks")
        url = hw.get("approval_url", "")
        if not url:
            self._set("approval", "off", "결재 대기함 주소 설정")
            return
        try:
            r = self.browser.read_count(url, compile_patterns(hw.get("approval_pattern", ""), DEFAULT_APPROVAL_PATTERNS),
                                        "headless" if hw.get("approval_headless", True) else "minimized")
        except BrowserError as e:
            self._set("approval", "error", str(e))
            return
        self._apply_read("approval", r, lambda n: setattr(self, "approval_wait", n))

    # ---------- 일정 ----------
    def _poll_calendar(self) -> None:
        url = self.vault.get("google.ical_url")
        if not url:
            self._set("calendar", "off", "Google iCal 주소 설정")
            return
        try:
            events = fetch_ical(url, self.clock())
        except urllib.error.HTTPError as e:
            self._set("calendar", "error", f"일정 주소 오류 ({e.code})")
            return
        except (urllib.error.URLError, OSError):
            self._set("calendar", "error", "일정을 가져오지 못했어요")
            return
        except ValueError as e:
            self._set("calendar", "error", str(e)[:40])
            return
        with self.lock:
            self.events = events
        self._set("calendar", "ok", "Google")

    # ---------- 할 일 ----------
    def _client(self) -> TodoistClient | None:
        token = self.vault.get("todoist.token")
        if not token:
            return None
        if self._todoist is None or self._todoist.token != token:
            self._todoist = TodoistClient(token)
        return self._todoist

    def _poll_todoist(self) -> None:
        c = self._client()
        if not c:
            self._set("todoist", "off", "Todoist 토큰 설정")
            return
        try:
            items = c.tasks(self._sec("todoist").get("filter") or "today | overdue")
        except urllib.error.HTTPError as e:
            self._set("todoist", "error", "토큰 확인 필요" if e.code in (401, 403) else f"Todoist 오류 ({e.code})")
            return
        except (urllib.error.URLError, OSError):
            self._set("todoist", "error", "Todoist 에 연결하지 못했어요")
            return
        todos = parse_todoist(items, self.clock())
        self.forget_gone({t.id for t in todos})
        with self.lock:
            self.todos = todos
        self._set("todoist", "ok", "Todoist")

    def complete_todo(self, task_id: str, on_done: Done | None = None) -> None:
        """누르면 바로 '완료' 로 보이고, UNDO_SEC 안에 다시 누르면 취소. 시간이 지나면 Todoist 에 반영."""
        self.mark_done(task_id)
        if on_done:
            on_done(True, "완료 — 8초 안에 다시 누르면 취소")

    def reopen_todo(self, task_id: str, on_done: Done | None = None) -> None:
        self.unmark_done(task_id)
        if on_done:
            on_done(True, "취소했어요")

    def commit_done(self, task_id: str) -> None:
        def run() -> None:
            c = self._client()
            try:
                if not c:
                    raise RuntimeError("no token")
                c.close(task_id)
            except Exception as e:
                log.warning("todoist close failed: %s", mask(repr(e), self.vault.values()))
                self.unmark_done(task_id)
                self.notice("할 일", ("Todoist 에 완료를 반영하지 못했어요",))
                return
            self._due["todoist"] = self.mono() + 2  # 잠시 뒤 목록 다시 받기
            self._wake.set()

        self.pool.submit(run)

    # ---------- 이카운트 · 브라우저 ----------
    def ecount_login(self, on_done: Done | None = None) -> None:
        v = self.vault
        url = self._sec("ecount").get("login_url") or "https://login.ecount.com/"

        def run() -> None:
            try:
                ok, msg = self.browser.ecount_login(url, v.get("ecount.company"), v.get("ecount.user"),
                                                    v.get("ecount.password"))
            except Exception as e:
                ok, msg = False, mask(str(e), v.values())[:60]
            if on_done:
                on_done(ok, msg)

        self.pool.submit(run)

    def open_hiworks_login(self, on_done: Done | None = None) -> None:
        self.open_work_url(self._sec("hiworks").get("mail_url") or HIWORKS_LOGIN, on_done)
        # 로그인하고 나면 바로 다시 읽도록 1분 뒤로 당긴다
        self._due["approval"] = self.mono() + 60

    def open_work_url(self, url: str, on_done: Done | None = None) -> None:
        def run() -> None:
            try:
                self.browser.open_visible(url)
                ok, msg = True, "업무 브라우저에서 열었어요"
            except Exception as e:
                ok, msg = open_url(url, self.settings.chrome_path), f"업무 브라우저 오류: {str(e)[:30]}"
            if on_done:
                on_done(ok, msg)

        self.pool.submit(run)

    # ---------- 화면 ----------
    def get(self, now: datetime) -> Snapshot:
        with self.lock:
            hw = self._sec("hiworks")
            return Snapshot(
                demo=False, mail_new=self.mail_new,
                mail_label="안 읽은 메일" if hw.get("mail_mode") == "web" else "새 메일",
                recent_mail=list(self.recent_mail), approval_wait=self.approval_wait, events=list(self.events),
                todos=self.apply_done(self.todos), states=dict(self.states))
