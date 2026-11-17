"""CharlieDeck 페이지 구성 (무엇을 보여줄지만 정한다. 그리기는 render.py).

D200 배치 (버튼 번호)
    [ 0][ 1][ 2][ 3][ 4]
    [ 5][ 6][ 7][ 8][ 9]      0~9  : 업무 버튼 (페이지마다 다름)
    [10][11][12][ 넓은 칸 ]   10/11/12 : 이전 / 홈 / 다음,  넓은 칸 : 날짜 · 시간 · 다음 일정 (길게: 설정 창)

페이지: 1 업무 홈 / 2 할 일 / 3 일정 / 4 하이웍스 / 5 바로가기

업무 홈
    [메일][결재 대기][다음 일정][이카운트 접속][오늘 할 일 n개]
    [할 일 1][할 일 2][할 일 3][할 일 4][할 일 5]      ← 누르면 완료 (8초 안에 다시 누르면 취소)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from .model import Event, Snapshot, SourceState, Todo
from .render import BLUE, CYAN, GREEN, ORANGE, PURPLE, RED, TEAL, YELLOW
from .settings import Settings, is_work_url

PAGES: list[tuple[str, str]] = [
    ("home", "업무 홈"),
    ("todo", "할 일"),
    ("schedule", "일정"),
    ("hiworks", "하이웍스"),
    ("links", "바로가기"),
]
SHORT_TITLE = {pid: title for pid, title in PAGES}
PAGE_COLOR = {"home": BLUE, "todo": RED, "schedule": PURPLE, "hiworks": CYAN, "links": TEAL}
NAV_PREV, NAV_HOME, NAV_NEXT = 10, 11, 12
WIDE_KEYS = (13, 14)  # 넓은 칸을 누르면 장치가 13 또는 14 로 보낸다
WEEKDAY_KO = "월화수목금토일"
PRIORITY_COLOR = {4: RED, 3: ORANGE, 2: BLUE, 1: "#7a809c"}  # Todoist API: 4 = p1(가장 급함)
LINK_COLORS = (TEAL, PURPLE, CYAN, GREEN, ORANGE, BLUE, YELLOW)


@dataclass(frozen=True)
class Key:
    kind: str = "blank"  # mail | approval | calendar | ecount | todo | goto | link | mail_ack | hiworks_login | refresh | setup | info | blank
    label: str = ""
    value: str = ""
    sub: str = ""
    color: str = BLUE
    icon: str = ""
    alert: bool = False  # 빨간 테두리 · 점
    warn: bool = False  # 노란 테두리 (로그인 필요)
    dim: bool = False
    gauge: float | None = None
    wrap: bool = False
    badge: str = ""
    highlight: bool = False
    target: str = ""  # link: 주소 / todo: 할 일 id / goto: 페이지 id
    detail: tuple[str, ...] = ()  # 누르면 넓은 칸에 보여줄 설명
    todo: Todo | None = None  # 할 일 칸이면 그림을 따로 그린다


BLANK = Key()


def num(v: int | None, unit: str = "") -> str:
    return "-" if v is None else f"{v:,}{unit}"


def with_state(k: Key, st: SourceState) -> Key:
    """연결 상태에 따라 칸 모양을 바꾼다 (설정 필요 · 확인 중 · 로그인 필요 · 오류)."""
    if st.status == "ok":
        return k
    base = replace(k, alert=False, warn=False, badge="", gauge=None, wrap=False, highlight=False)
    if st.status == "off":
        return replace(base, value="설정", sub=st.message or "설정 필요", dim=True)
    if st.status == "loading":
        return replace(base, value="…", sub=st.message or "확인 중", dim=True)
    if st.status == "login":
        return replace(base, value="로그인", sub="눌러서 로그인", warn=True)
    return replace(base, value="!", sub=st.message or "오류", alert=True)


def event_badge(ev: Event, now: datetime) -> str:
    st = ev.state(now)
    if st == "now":
        return "진행 중"
    if st == "past":
        return "완료"
    if ev.start.date() != now.date():
        days = (ev.start.date() - now.date()).days
        return "내일" if days == 1 else f"{ev.start.month}/{ev.start.day}"
    mins = int((ev.start - now).total_seconds() // 60)
    return f"{max(1, mins)}분 후" if mins < 60 else f"{mins // 60}시간 후"


def todo_key(t: Todo) -> Key:
    return Key("todo", t.due or "오늘", t.content, color=PRIORITY_COLOR.get(t.priority, BLUE), target=t.id, todo=t,
               detail=(t.content,))


# ---------- 공통 칸 ----------
def mail_key(s: Snapshot) -> Key:
    first = s.recent_mail[0][1] if s.recent_mail else ""
    k = Key("mail", s.mail_label, num(s.mail_new), first or ("마지막 확인 뒤" if s.mail_label == "새 메일" else "메일함"),
            CYAN, "mail", alert=bool(s.mail_new), detail=tuple(f"{a} · {b}" for a, b in s.recent_mail[:3]))
    return with_state(k, s.state("mail"))


def approval_key(s: Snapshot) -> Key:
    k = Key("approval", "결재 대기", num(s.approval_wait), "결재 대기함", YELLOW, "approve", alert=bool(s.approval_wait))
    return with_state(k, s.state("approval"))


def calendar_key(s: Snapshot, now: datetime) -> Key:
    today = [e for e in s.today(now) if not e.all_day]
    nxt = s.next_event(now)
    if nxt:
        place = f" · {nxt.place}" if nxt.place else ""
        k = Key("calendar", "다음 일정", nxt.title, f"{nxt.start:%H:%M}{place}", PURPLE, "calendar", wrap=True,
                badge=event_badge(nxt, now), highlight=nxt.state(now) in ("now", "soon"),
                detail=(f"{nxt.start:%H:%M}–{nxt.end:%H:%M}", nxt.title, nxt.place))
    else:
        k = Key("calendar", "오늘 일정", "일정 없음", f"오늘 {len(today)}건", PURPLE, "calendar")
    return with_state(k, s.state("calendar"))


def ecount_key(s: Snapshot) -> Key:
    return with_state(Key("ecount", "이카운트", "접속", "자동 로그인", GREEN, "chart"), s.state("ecount"))


# ---------- 페이지별 ----------
def page_home(s: Snapshot, now: datetime, settings: Settings) -> list[Key]:  # noqa: ARG001
    todos = s.todos
    left = [t for t in todos if not t.done]
    overdue = sum(1 for t in left if t.overdue)
    st = s.state("todoist")
    summary = with_state(Key("goto", "오늘 할 일", f"{len(left)}개", f"지연 {overdue}개" if overdue else "전체 보기",
                             RED if overdue else TEAL, "check", alert=bool(overdue), target="todo"), st)
    if st.status in ("off", "error"):  # 설정이 안 됐으면 누르면 설정 창
        summary = replace(summary, kind="setup")
    keys = [mail_key(s), approval_key(s), calendar_key(s, now), ecount_key(s), summary]
    # 아래 줄: 할 일 5개 (연결 전에는 위 칸이 안내하므로 비워 둔다)
    keys += todo_row(s, 5) if st.status == "ok" else [BLANK] * 5
    return keys


def todo_row(s: Snapshot, n: int) -> list[Key]:
    st = s.state("todoist")
    if st.status != "ok":
        first = with_state(Key("setup", "Todoist", "", "", RED, "check"), st)
        return [replace(first, kind="setup" if st.status in ("off", "error") else "info")] + [BLANK] * (n - 1)
    if not s.todos:
        return [Key("info", "할 일", "모두 끝!", "오늘 할 일 없음", GREEN, "check")] + [BLANK] * (n - 1)
    keys = [todo_key(t) for t in s.todos[:n]]
    if len(s.todos) > n:  # 마지막 칸은 '더 있음'
        rest = len(s.todos) - (n - 1)
        keys[-1] = Key("goto", "할 일", f"+{rest}개", "할 일 페이지", RED, "check", target="todo")
    return keys + [BLANK] * (n - len(keys))


def page_todo(s: Snapshot, now: datetime, settings: Settings) -> list[Key]:  # noqa: ARG001
    keys = todo_row(s, 10)
    if s.state("todoist").status == "ok" and len(s.todos) > 10:
        keys[9] = Key("link", "Todoist", f"+{len(s.todos) - 9}개", "웹에서 보기", RED, "check",
                      target=settings.section("todoist").get("web_url", ""))
    return keys


def page_schedule(s: Snapshot, now: datetime, settings: Settings) -> list[Key]:
    st = s.state("calendar")
    web = settings.section("google").get("calendar_web", "")
    keys: list[Key] = []
    if st.status != "ok":
        keys.append(replace(with_state(Key("calendar", "오늘 일정", "", "", PURPLE, "calendar"), st)))
    else:
        today = s.today(now)
        shown = today[:5] if len(today) <= 5 else today[:4]
        for ev in shown:
            if ev.all_day:
                keys.append(Key("link", "종일", ev.title, ev.place, TEAL, "calendar", wrap=True, target=web,
                                detail=("종일", ev.title, ev.place)))
                continue
            stt = ev.state(now)
            keys.append(Key("link", f"{ev.start:%H:%M}–{ev.end:%H:%M}", ev.title, ev.place,
                            GREEN if stt == "now" else YELLOW if stt == "soon" else PURPLE, "clock", wrap=True,
                            badge=event_badge(ev, now), dim=stt == "past", highlight=stt in ("now", "soon"),
                            target=web))
        if len(today) > 5:
            keys.append(Key("link", "일정", f"+{len(today) - 4}건", "캘린더에서 보기", PURPLE, "calendar", target=web))
        if not today:
            keys.append(Key("link", "오늘 일정", "일정 없음", f"{now.month}/{now.day} ({WEEKDAY_KO[now.weekday()]})",
                            PURPLE, "calendar", target=web))
    keys += [BLANK] * (5 - len(keys))
    monday = now.date() - timedelta(days=now.weekday())
    counts = s.week_counts(now)
    for k in range(5):
        day = monday + timedelta(days=k)
        is_today = day == now.date()
        label = f"{WEEKDAY_KO[k]} {day.month}/{day.day}"
        if st.status != "ok":
            keys.append(Key("info", label, "-", "", BLUE, dim=True, highlight=is_today))
            continue
        keys.append(Key("link", label, f"{counts[k]}건", "오늘" if is_today else "", PURPLE if is_today else BLUE,
                        badge="오늘" if is_today else "", highlight=is_today, dim=day < now.date(), target=web))
    return keys


def page_hiworks(s: Snapshot, now: datetime, settings: Settings) -> list[Key]:  # noqa: ARG001
    hw = settings.section("hiworks")
    mail_url, appr_url = hw.get("mail_url", ""), hw.get("approval_url", "")
    keys = [
        mail_key(s),
        approval_key(s),
        Key("link", "메일함", "열기", "업무 브라우저" if mail_url else "주소 설정 필요", CYAN, "mail", target=mail_url,
            dim=not mail_url),
        Key("link", "결재함", "열기", "업무 브라우저" if appr_url else "주소 설정 필요", YELLOW, "approve",
            target=appr_url, dim=not appr_url),
        Key("hiworks_login", "하이웍스", "로그인", "업무 브라우저 창", BLUE, "user",
            warn=s.state("approval").status == "login"),
    ]
    for i in range(3):
        if i < len(s.recent_mail) and s.state("mail").status == "ok":
            sender, subject = s.recent_mail[i]
            keys.append(Key("link", sender or f"메일 {i + 1}", subject, "새 메일", BLUE, "doc", wrap=True,
                            target=mail_url, detail=(sender, subject)))
        else:
            keys.append(Key("info", f"최근 메일 {i + 1}", "-", "", BLUE, "doc", dim=True))
    keys.append(Key("mail_ack", "메일", "확인", "새 메일 수 0 으로", TEAL, "check"))
    keys.append(Key("refresh", "새로고침", "지금", "모두 다시 확인", TEAL, "refresh"))
    return keys


def _link_icon(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for word, icon in (("genspark", "sparkle"), ("todoist", "check"), ("calendar", "calendar"), ("hiworks", "mail"),
                       ("ecount", "chart"), ("drive", "folder"), ("docs", "doc"), ("sheets", "sheet")):
        if word in host or word in url.lower():
            return icon
    return "browser"


def page_links(s: Snapshot, now: datetime, settings: Settings) -> list[Key]:  # noqa: ARG001
    links = settings.links()
    keys = []
    for i, item in enumerate(links):
        url = item["url"]
        keys.append(Key("link", item["name"], item["name"], "업무 브라우저" if is_work_url(url) else "Chrome",
                        LINK_COLORS[i % len(LINK_COLORS)], _link_icon(url), target=url))
    if len(keys) < 10:
        keys.append(Key("setup", "바로가기", "추가", "설정 창에서", "#565f89", "plus"))
    return (keys + [BLANK] * 10)[:10]


BUILDERS = {"home": page_home, "todo": page_todo, "schedule": page_schedule, "hiworks": page_hiworks,
            "links": page_links}


def build(page: int, snap: Snapshot, now: datetime, settings: Settings) -> list[Key]:
    """페이지 번호(0부터)의 업무 버튼 10개."""
    pid = PAGES[page % len(PAGES)][0]
    return (BUILDERS[pid](snap, now, settings) + [BLANK] * 10)[:10]


def page_index(pid: str) -> int:
    return next(i for i, (p, _) in enumerate(PAGES) if p == pid)
