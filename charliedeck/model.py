"""화면에 그릴 업무 데이터.

공급자(Provider)
- DemoProvider : demo_data.json 의 가짜 데이터 (--demo). 할 일 완료 · 메일 확인도 흉내 낸다.
- LiveProvider : 실제 연결 (live.py) — 하이웍스 POP3 · 결재 대기(전용 Chrome) · Google 일정(iCal) · Todoist · 이카운트

둘 다 같은 Snapshot 을 돌려주므로 화면 코드는 공급자를 모른다.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Callable

DEMO_FILE = Path(__file__).with_name("demo_data.json")
UNDO_SEC = 8  # 할 일을 완료한 뒤 같은 칸을 다시 누르면 되돌릴 수 있는 시간
SOURCES = ("mail", "approval", "calendar", "todoist", "ecount")

Done = Callable[[bool, str], None]  # (성공 여부, 안내 문구)


@dataclass(frozen=True)
class Event:
    start: datetime
    end: datetime
    title: str
    place: str = ""
    all_day: bool = False

    def state(self, now: datetime) -> str:
        """past | now | soon(30분 안) | later"""
        if now >= self.end:
            return "past"
        if now >= self.start:
            return "now"
        if self.start - now <= timedelta(minutes=30):
            return "soon"
        return "later"


@dataclass(frozen=True)
class Todo:
    id: str
    content: str
    due: str = ""  # 화면용: "14:00" | "오늘" | "지연 11/15"
    priority: int = 1  # Todoist API 기준 4 = 가장 급함(p1), 1 = 보통(p4)
    overdue: bool = False
    done: bool = False  # 방금 완료함 (UNDO_SEC 동안 '완료' 로 보이고 다시 누르면 취소)


@dataclass(frozen=True)
class SourceState:
    """연결 하나의 상태. status: off(설정 필요) | loading | ok | login(로그인 필요) | error"""

    status: str = "off"
    message: str = ""
    action: str = "setup"  # 칸을 눌렀을 때: setup(설정 창) | hiworks(하이웍스 로그인 창)


@dataclass
class Snapshot:
    demo: bool
    mail_new: int | None = None
    mail_label: str = "새 메일"  # POP3: 새 메일 / 웹: 안 읽은 메일
    recent_mail: list[tuple[str, str]] = field(default_factory=list)  # (보낸 사람, 제목)
    approval_wait: int | None = None
    events: list[Event] = field(default_factory=list)  # 이번 주 월요일 ~ 앞으로 8일
    todos: list[Todo] = field(default_factory=list)
    states: dict[str, SourceState] = field(default_factory=dict)

    def state(self, name: str) -> SourceState:
        return self.states.get(name, SourceState())

    def today(self, now: datetime) -> list[Event]:
        return sorted((e for e in self.events if e.start.date() <= now.date() < (e.end - timedelta(seconds=1)).date()
                       or e.start.date() == now.date()), key=lambda e: (not e.all_day, e.start))

    def later(self, now: datetime) -> list[Event]:
        return sorted((e for e in self.events if e.start.date() > now.date()), key=lambda e: e.start)

    def next_event(self, now: datetime) -> Event | None:
        """진행 중이거나 다음에 시작할 시간 일정 (오늘 없으면 다음 날 첫 일정). 종일 일정은 건너뛴다."""
        for ev in self.today(now):
            if not ev.all_day and ev.end > now:
                return ev
        return next((e for e in self.later(now) if not e.all_day), None)

    def upcoming(self, now: datetime, limit: int = 3) -> list[Event]:
        items = [e for e in self.today(now) if not e.all_day and e.end > now] + \
                [e for e in self.later(now) if not e.all_day]
        return items[:limit]

    def week_counts(self, now: datetime) -> list[int]:
        monday = now.date() - timedelta(days=now.weekday())
        return [sum(1 for e in self.events if e.start.date() == monday + timedelta(days=k)) for k in range(5)]


# ---------- 공통: 할 일 완료 되돌리기 ----------
class ProviderBase:
    """할 일 '완료' 표시(UNDO_SEC) · 화면 버전 관리. Demo/Live 공통."""

    demo = False

    def __init__(self, monotonic: Callable[[], float] | None = None) -> None:
        self.mono = monotonic or time.monotonic
        self.lock = threading.RLock()
        self.version = 0
        self._done: dict[str, float] = {}  # 할 일 id → 되돌리기 마감 시각
        self._gone: set[str] = set()  # 되돌리기 시간이 지나 화면에서 뺀 할 일

    def bump(self) -> None:
        with self.lock:
            self.version += 1

    def mark_done(self, task_id: str) -> None:
        with self.lock:
            self._done[task_id] = self.mono() + UNDO_SEC
            self.bump()

    def unmark_done(self, task_id: str) -> None:
        with self.lock:
            self._done.pop(task_id, None)
            self._gone.discard(task_id)
            self.bump()

    def expire(self) -> None:
        with self.lock:
            now = self.mono()
            for tid, until in list(self._done.items()):
                if now >= until:
                    del self._done[tid]
                    self._gone.add(tid)
                    self.bump()
                    self.commit_done(tid)

    def apply_done(self, todos: list[Todo]) -> list[Todo]:
        out = []
        with self.lock:
            for t in todos:
                if t.id in self._gone:
                    continue
                out.append(replace(t, done=True) if t.id in self._done else t)
        return out

    def forget_gone(self, present_ids: set[str]) -> None:
        """새로 받은 목록에 없는 id 는 기억할 필요가 없다 (완료가 서버에 반영됨)."""
        with self.lock:
            self._gone &= present_ids

    # 기본 동작 (공급자마다 덮어씀)
    def commit_done(self, task_id: str) -> None:  # noqa: ARG002
        """되돌리기 시간이 지난 완료를 실제로 반영 (Live: Todoist 에 완료 요청)."""

    def drain_notices(self) -> list[tuple[str, tuple[str, ...], str]]:
        return []

    def reload(self, settings) -> None:  # noqa: ARG002
        """설정이 바뀌었을 때 (셋업 창 저장)."""

    def poll(self, force: bool = False) -> None:  # noqa: ARG002
        self.expire()

    def close(self) -> None: ...


# ---------- demo ----------
def _at(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, dtime(h, m))


class DemoProvider(ProviderBase):
    """가짜 데이터. 날짜는 '오늘' 기준으로 맞춘다. 실제 계정·회사 정보가 아니다."""

    name = "demo"
    demo = True

    def __init__(self, path: Path = DEMO_FILE, monotonic: Callable[[], float] | None = None) -> None:
        super().__init__(monotonic)
        with open(path, encoding="utf-8") as fp:
            self.raw = json.load(fp)
        self.mail_new = int(self.raw.get("mail_new", 0))
        self.ecount_log: list[str] = []

    def _events(self, now: datetime) -> list[Event]:
        today = now.date()
        nxt = today + timedelta(days=1)
        while nxt.weekday() >= 5:  # 주말 건너뛰기
            nxt += timedelta(days=1)
        monday = today - timedelta(days=today.weekday())
        out: list[Event] = []
        if today.weekday() < 5:
            out += [Event(_at(today, e["start"]), _at(today, e["end"]), e["title"], e.get("place", ""))
                    for e in self.raw.get("events_today", [])]
        out += [Event(_at(nxt, e["start"]), _at(nxt, e["end"]), e["title"], e.get("place", ""))
                for e in self.raw.get("events_next", [])]
        for k, n in enumerate(self.raw.get("week_counts", [])[:5]):  # 다른 요일 일정 수만 채우는 가짜 일정
            day = monday + timedelta(days=k)
            if day in (today, nxt):
                continue
            out += [Event(_at(day, "10:00"), _at(day, "11:00"), f"일정 {i + 1}") for i in range(n)]
        return out

    def get(self, now: datetime) -> Snapshot:
        todos = [Todo(str(t["id"]), t["content"], t.get("due", "오늘"), int(t.get("priority", 1)),
                      bool(t.get("overdue"))) for t in self.raw.get("todos", [])]
        ok = SourceState("ok")
        return Snapshot(
            demo=True, mail_new=self.mail_new, mail_label="새 메일",
            recent_mail=[(m["from"], m["subject"]) for m in self.raw.get("recent_mail", [])],
            approval_wait=self.raw.get("approval_wait"), events=self._events(now), todos=self.apply_done(todos),
            states={k: ok for k in SOURCES})

    # 동작 흉내
    def complete_todo(self, task_id: str, on_done: Done | None = None) -> None:
        self.mark_done(task_id)
        if on_done:
            on_done(True, "demo: 완료했어요")

    def reopen_todo(self, task_id: str, on_done: Done | None = None) -> None:
        self.unmark_done(task_id)
        if on_done:
            on_done(True, "demo: 되돌렸어요")

    def ack_mail(self) -> None:
        self.mail_new = 0
        self.bump()

    def ecount_login(self, on_done: Done | None = None) -> None:
        self.ecount_log.append("login")
        if on_done:
            on_done(True, "demo: 실제로 접속하지 않았어요")

    def open_hiworks_login(self, on_done: Done | None = None) -> None:
        if on_done:
            on_done(True, "demo: 실제로 열지 않았어요")

    def open_work_url(self, url: str, on_done: Done | None = None) -> None:  # noqa: ARG002
        if on_done:
            on_done(True, "demo: 실제로 열지 않았어요")
