"""CharlieDeck 설정 창 (Tkinter, 추가 설치 없음).

여는 방법: 처음 실행할 때 자동 / D200 넓은 칸 길게 / 시뮬레이터 '설정' 단추 / CharlieDeck --setup

저장 위치
- 주소 · 주기 · 밝기 등 일반 설정 → %APPDATA%\\CharlieDeck\\charliedeck.json
- 계정 · 비밀번호 · 토큰 · iCal 주소 → Windows 자격 증명 관리자 (vault.py)
  저장된 비밀 값은 다시 보여주지 않는다. 빈칸으로 두면 그대로, '지우기' 를 누르면 삭제.

실행 중인 CharlieDeck 은 저장을 감지해 몇 초 안에 새 설정을 적용한다.
"""

from __future__ import annotations

import copy
import socket
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from . import settings as settings_mod
from .vault import KEYS, Vault, VaultUnavailable, mask

CLEAR = "\x00clear"  # 비밀 값 '지우기' 표시
SETUP_LOCK_PORT = 47232

# (탭, 필드 id, 이름, 종류, 도움말)
# 종류: str | int | bool | choice:a,b | secret | text   /   id: "cfg:섹션.키" 또는 "vault:키"
FIELDS: list[tuple[str, str, str, str, str]] = [
    ("하이웍스", "vault:hiworks.user", "메일 계정", "str", "하이웍스 메일 주소 전체 (POP3 로그인 아이디)"),
    ("하이웍스", "vault:hiworks.password", "메일 비밀번호", "secret", "자격 증명 관리자에 암호화 저장"),
    ("하이웍스", "cfg:hiworks.mail_mode", "메일 숫자", "choice:pop3,web",
     "pop3 = 마지막 확인 뒤 새 메일 수 / web = 웹 메일 화면의 '안 읽은 메일' 숫자 (업무 브라우저)"),
    ("하이웍스", "cfg:hiworks.pop3_host", "POP3 서버", "str", "기본 pop3s.hiworks.com"),
    ("하이웍스", "cfg:hiworks.pop3_port", "POP3 포트", "int", "기본 995 (SSL)"),
    ("하이웍스", "cfg:hiworks.mail_poll_min", "메일 확인 주기(분)", "int", ""),
    ("하이웍스", "cfg:hiworks.mail_url", "메일함 주소", "str", "업무 브라우저로 하이웍스 메일함을 연 뒤 주소창 주소를 붙여 넣기"),
    ("하이웍스", "cfg:hiworks.approval_url", "결재 대기함 주소", "str", "업무 브라우저로 결재 대기함을 연 뒤 주소창 주소를 붙여 넣기"),
    ("하이웍스", "cfg:hiworks.approval_poll_min", "결재 확인 주기(분)", "int", ""),
    ("하이웍스", "cfg:hiworks.approval_headless", "창 없이 읽기", "bool", "끄면 최소화된 창으로 읽음 (화면 없이 읽기가 막힐 때)"),
    ("하이웍스", "cfg:hiworks.approval_pattern", "결재 숫자 규칙(고급)", "str",
     "비우면 기본. 예: 결재\\s*대기\\D*(\\d+)  — 숫자 자리는 (\\d+)"),
    ("일정", "vault:google.ical_url", "비공개 iCal 주소", "secret",
     "Google 캘린더 → 설정 → 내 캘린더 → 캘린더 통합 → 'iCal 형식의 비공개 주소'"),
    ("일정", "cfg:google.poll_min", "확인 주기(분)", "int", ""),
    ("일정", "cfg:google.calendar_web", "캘린더 웹 주소", "str", "일정 칸을 눌렀을 때 열 주소"),
    ("할 일", "vault:todoist.token", "Todoist API 토큰", "secret",
     "Todoist → 설정 → 연동 → 개발자 → API 토큰 복사"),
    ("할 일", "cfg:todoist.filter", "필터", "str", "기본 today | overdue (오늘 + 지연)"),
    ("할 일", "cfg:todoist.poll_min", "확인 주기(분)", "int", ""),
    ("할 일", "cfg:todoist.web_url", "Todoist 웹 주소", "str", ""),
    ("이카운트", "vault:ecount.company", "회사코드", "str", ""),
    ("이카운트", "vault:ecount.user", "아이디", "str", ""),
    ("이카운트", "vault:ecount.password", "비밀번호", "secret", "덱에서 누르면 업무 브라우저에 자동 입력 · 로그인"),
    ("이카운트", "cfg:ecount.login_url", "로그인 주소", "str", "기본 https://login.ecount.com/"),
    ("바로가기", "cfg:links", "바로가기 (최대 10개)", "text",
     "한 줄에 '이름 | 주소'. 하이웍스·이카운트 주소는 로그인된 업무 브라우저로 열려요"),
    ("일반", "cfg:brightness", "밝기(%)", "int", "20 · 50 · 80 · 100 (홈 버튼 길게 눌러도 바뀜)"),
    ("일반", "cfg:long_press_sec", "길게 누르기(초)", "str", "기본 0.8"),
    ("일반", "cfg:chrome_path", "Chrome 경로", "str", "비우면 자동으로 찾아요 (Chrome → Edge)"),
]
TABS = ("하이웍스", "일정", "할 일", "이카운트", "바로가기", "일반")


def _get_cfg(data: dict, path: str):
    if path == "links":
        return settings_mod.format_links(data.get("links") or [])
    sec, _, key = path.partition(".")
    return (data.get(sec) or {}).get(key) if key else data.get(sec)


def _set_cfg(data: dict, path: str, value) -> None:
    if path == "links":
        data["links"] = settings_mod.parse_links(str(value))
        return
    sec, _, key = path.partition(".")
    if key:
        data.setdefault(sec, {})[key] = value
    else:
        data[sec] = value


def initial_values(data: dict, vault: Vault) -> dict[str, object]:
    """창을 열 때 칸에 넣을 값 (비밀 값은 빈칸)."""
    out: dict[str, object] = {}
    for _, fid, _, kind, _ in FIELDS:
        src, _, path = fid.partition(":")
        if src == "cfg":
            out[fid] = _get_cfg(data, path)
        elif kind == "secret":
            out[fid] = ""
        else:
            out[fid] = vault.get(path)
    return out


def apply_form(form: dict[str, object], data: dict, vault: Vault) -> tuple[dict, list[str]]:
    """폼 값 → (저장할 설정 dict, 바뀐 비밀 항목 이름들). 설정 dict 는 settings.save 로 저장한다.

    비밀 칸: 빈 값 = 그대로 / CLEAR = 삭제 / 그 밖 = 새 값.  일반 vault 칸: 빈 값이면 삭제.
    """
    new = copy.deepcopy(data)
    changed: list[str] = []
    for _, fid, label, kind, _ in FIELDS:
        if fid not in form:
            continue
        src, _, path = fid.partition(":")
        value = form[fid]
        if src == "vault":
            if kind == "secret":
                if value == CLEAR:
                    vault.delete(path)
                    changed.append(label)
                elif isinstance(value, str) and value.strip():
                    vault.set(path, value.strip())
                    changed.append(label)
            else:
                value = str(value or "").strip()
                if value != vault.get(path):
                    vault.set(path, value)
                    changed.append(label)
            continue
        if kind == "int":
            try:
                value = int(str(value).strip())
            except ValueError:
                continue
        elif kind == "bool":
            value = bool(value)
        elif kind != "text":
            value = str(value or "").strip()
        _set_cfg(new, path, value)
    return new, changed


def form_secret(form: dict[str, object], vault: Vault, key: str) -> str:
    """테스트 단추용: 새로 입력한 값이 있으면 그것, 없으면 저장된 값."""
    v = form.get(f"vault:{key}")
    if v == CLEAR:
        return ""
    if isinstance(v, str) and v.strip():
        return v.strip()
    return vault.get(key)


def form_cfg(form: dict[str, object], data: dict, path: str):
    return form.get(f"cfg:{path}", _get_cfg(data, path))


# ---------- 연결 테스트 (창 밖에서도 쓸 수 있게 함수로) ----------
def test_mail(form, data, vault) -> str:
    from .live import Pop3Mail

    user, pw = form_secret(form, vault, "hiworks.user"), form_secret(form, vault, "hiworks.password")
    if not (user and pw):
        return "메일 계정과 비밀번호를 입력하세요"
    with tempfile.TemporaryDirectory() as tmp:
        box = Pop3Mail(str(form_cfg(form, data, "hiworks.pop3_host") or "pop3s.hiworks.com"),
                       int(form_cfg(form, data, "hiworks.pop3_port") or 995), user, pw,
                       seen_path=Path(tmp) / "seen.json")
        box.check()
        return f"연결 성공 — 서버에 메일 {len(box.current)}통 (메일은 지우지 않아요)"


def test_calendar(form, data, vault) -> str:  # noqa: ARG001
    from .live import fetch_ical

    url = form_secret(form, vault, "google.ical_url")
    if not url:
        return "iCal 주소를 입력하세요"
    now = datetime.now()
    events = fetch_ical(url, now)
    today = [e for e in events if e.start.date() == now.date()]
    return f"연결 성공 — 오늘 일정 {len(today)}건, 이번 주 {len(events)}건"


def test_todoist(form, data, vault) -> str:
    from .live import TodoistClient

    token = form_secret(form, vault, "todoist.token")
    if not token:
        return "Todoist 토큰을 입력하세요"
    items = TodoistClient(token).tasks(str(form_cfg(form, data, "todoist.filter") or "today | overdue"))
    return f"연결 성공 — 할 일 {len(items)}개"


def test_approval(form, data, vault) -> str:  # noqa: ARG001
    from .browser import DEFAULT_APPROVAL_PATTERNS, WorkBrowser, compile_patterns

    url = str(form_cfg(form, data, "hiworks.approval_url") or "")
    if not url:
        return "결재 대기함 주소를 입력하세요"
    b = WorkBrowser(str(form_cfg(form, data, "chrome_path") or ""))
    r = b.read_count(url, compile_patterns(str(form_cfg(form, data, "hiworks.approval_pattern") or ""),
                                           DEFAULT_APPROVAL_PATTERNS),
                     "headless" if form_cfg(form, data, "hiworks.approval_headless") else "minimized")
    if r.status == "ok":
        return f"결재 대기 {r.count}건 — 읽은 부분: {r.snippet[:50]}"
    if r.status == "login":
        return "로그인이 필요해요 → '하이웍스 로그인' 을 먼저 누르세요"
    return f"숫자를 못 찾았어요. 화면 글자: {r.snippet[:60] or '(없음)'} → 고급 규칙을 넣어 보세요"


def open_hiworks(form, data, vault) -> str:  # noqa: ARG001
    from .browser import HIWORKS_LOGIN, WorkBrowser

    WorkBrowser(str(form_cfg(form, data, "chrome_path") or "")).open_visible(
        str(form_cfg(form, data, "hiworks.mail_url") or "") or HIWORKS_LOGIN)
    return "업무 브라우저를 열었어요 — 로그인 후 메일함·결재 대기함 주소를 복사해 넣으세요"


def test_ecount(form, data, vault) -> str:
    from .browser import WorkBrowser

    ok, msg = WorkBrowser(str(form_cfg(form, data, "chrome_path") or "")).ecount_login(
        str(form_cfg(form, data, "ecount.login_url") or "https://login.ecount.com/"),
        form_secret(form, vault, "ecount.company"), form_secret(form, vault, "ecount.user"),
        form_secret(form, vault, "ecount.password"))
    return msg


TAB_ACTIONS = {
    "하이웍스": [("메일 연결 테스트", test_mail), ("하이웍스 로그인 (업무 브라우저)", open_hiworks),
              ("결재 숫자 읽기 테스트", test_approval)],
    "일정": [("일정 가져오기 테스트", test_calendar)],
    "할 일": [("Todoist 테스트", test_todoist)],
    "이카운트": [("지금 접속해 보기", test_ecount)],
}


def single_instance() -> socket.socket | None:
    """설정 창은 하나만. 이미 열려 있으면 None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SETUP_LOCK_PORT))
        return s
    except OSError:
        s.close()
        return None


# ---------- 창 ----------
def run(config_path: str | None = None) -> int:
    import tkinter as tk  # noqa: PLC0415
    from tkinter import messagebox, ttk  # noqa: PLC0415

    lock = single_instance()
    if lock is None:
        print("설정 창이 이미 열려 있어요")
        return 0
    s = settings_mod.load(config_path)
    try:
        vault = Vault()
        vault_note = "계정·비밀번호·토큰은 Windows 자격 증명 관리자에 암호화 저장돼요."
    except VaultUnavailable as e:
        from .vault import MemoryBackend

        vault = Vault(MemoryBackend())
        vault_note = f"⚠ {e} — 이번 실행 동안만 기억해요."
    data = s.data

    root = tk.Tk()
    root.title("CharlieDeck 설정")
    root.geometry("760x620")
    root.minsize(680, 560)
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    head = ttk.Frame(root, padding=(14, 12, 14, 4))
    head.pack(fill="x")
    ttk.Label(head, text="CharlieDeck 설정", font=("Malgun Gothic", 15, "bold")).pack(anchor="w")
    ttk.Label(head, text=vault_note + " 저장된 비밀 값은 다시 보여주지 않아요 (빈칸 = 그대로).",
              foreground="#555", wraplength=720).pack(anchor="w", pady=(2, 0))
    for w in s.warnings:
        ttk.Label(head, text="⚠ " + w, foreground="#b55", wraplength=720).pack(anchor="w")

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=12, pady=6)
    widgets: dict[str, tuple[str, object]] = {}
    cleared: set[str] = set()
    init = initial_values(data, vault)
    status = tk.StringVar(value="")

    def collect() -> dict[str, object]:
        form: dict[str, object] = {}
        for fid, (kind, w) in widgets.items():
            if kind == "bool":
                form[fid] = bool(w.get())
            elif kind == "text":
                form[fid] = w.get("1.0", "end").strip()
            elif kind == "secret":
                form[fid] = CLEAR if fid in cleared and not w.get().strip() else w.get()
            else:
                form[fid] = w.get()
        return form

    def run_action(fn, label) -> None:
        status.set(f"{label} … 잠시만요")
        form = collect()

        def work() -> None:
            try:
                msg = fn(form, data, vault)
            except Exception as e:  # 네트워크 · 브라우저 오류를 그대로 보여주되 비밀 값은 가린다
                msg = f"실패: {mask(str(e), vault.values() + [str(v) for v in form.values() if isinstance(v, str)])}"
            root.after(0, lambda: status.set(f"{label}: {msg[:160]}"))

        threading.Thread(target=work, daemon=True).start()

    for tab in TABS:
        frame = ttk.Frame(nb, padding=12)
        nb.add(frame, text=tab)
        frame.columnconfigure(1, weight=1)
        row = 0
        for t_, fid, label, kind, help_ in FIELDS:
            if t_ != tab:
                continue
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=(6, 0))
            if kind == "bool":
                var = tk.BooleanVar(value=bool(init[fid]))
                ttk.Checkbutton(frame, variable=var).grid(row=row, column=1, sticky="w", pady=(6, 0))
                widgets[fid] = (kind, var)
            elif kind.startswith("choice:"):
                cb = ttk.Combobox(frame, values=kind[7:].split(","), state="readonly", width=12)
                cb.set(str(init[fid] or kind[7:].split(",")[0]))
                cb.grid(row=row, column=1, sticky="w", pady=(6, 0))
                widgets[fid] = (kind, cb)
            elif kind == "text":
                txt = tk.Text(frame, height=11, font=("Consolas", 10), wrap="none")
                txt.insert("1.0", str(init[fid] or ""))
                txt.grid(row=row, column=1, sticky="nsew", pady=(6, 0))
                frame.rowconfigure(row, weight=1)
                widgets[fid] = (kind, txt)
            else:
                ent = ttk.Entry(frame, show="●" if kind == "secret" else "")
                if kind != "secret":
                    ent.insert(0, "" if init[fid] is None else str(init[fid]))
                ent.grid(row=row, column=1, sticky="ew", pady=(6, 0))
                widgets[fid] = (kind, ent)
                if kind == "secret":
                    key = fid.split(":", 1)[1]
                    saved = tk.StringVar(value="●●●● 저장됨" if vault.has(key) else "저장 안 됨")
                    ttk.Label(frame, textvariable=saved, foreground="#2a7" if vault.has(key) else "#999").grid(
                        row=row, column=2, sticky="w", padx=6, pady=(6, 0))

                    def clear(f=fid, var=saved, e=ent) -> None:
                        cleared.add(f)
                        e.delete(0, "end")
                        var.set("저장하면 지워져요")

                    ttk.Button(frame, text="지우기", width=7, command=clear).grid(row=row, column=3, pady=(6, 0))
            row += 1
            if help_:
                ttk.Label(frame, text=help_, foreground="#777", wraplength=520).grid(row=row, column=1, columnspan=3,
                                                                                     sticky="w")
                row += 1
        if tab in TAB_ACTIONS:
            bar = ttk.Frame(frame)
            bar.grid(row=row, column=0, columnspan=4, sticky="w", pady=(14, 0))
            for label, fn in TAB_ACTIONS[tab]:
                ttk.Button(bar, text=label, command=lambda f=fn, lb=label: run_action(f, lb)).pack(side="left",
                                                                                                padx=(0, 8))
        if tab == "일반":
            from . import startup  # noqa: PLC0415

            auto = tk.BooleanVar(value=startup.entry_path().exists())
            ttk.Checkbutton(frame, text="Windows 로그인할 때 CharlieDeck 자동 실행", variable=auto).grid(
                row=row, column=0, columnspan=3, sticky="w", pady=(14, 0))
            widgets["startup"] = ("bool", auto)
            row += 1
            from platform_util import config_dir, log_dir, open_path  # noqa: PLC0415

            bar = ttk.Frame(frame)
            bar.grid(row=row, column=0, columnspan=4, sticky="w", pady=(10, 0))
            ttk.Button(bar, text="설정 폴더 열기", command=lambda: open_path(str(config_dir()))).pack(side="left",
                                                                                               padx=(0, 8))
            ttk.Button(bar, text="로그 폴더 열기", command=lambda: open_path(str(log_dir()))).pack(side="left")

    foot = ttk.Frame(root, padding=(12, 0, 12, 12))
    foot.pack(fill="x")
    ttk.Label(foot, textvariable=status, foreground="#246", wraplength=500).pack(side="left", fill="x", expand=True)

    def save(close: bool) -> None:
        form = collect()
        auto = form.pop("startup", None)
        new, changed = apply_form(form, data, vault)
        path = settings_mod.save(new, config_path)
        data.clear()
        data.update(settings_mod.load(path).data)
        if auto is not None:
            from . import startup  # noqa: PLC0415

            try:
                startup.install() if auto else startup.uninstall()
            except RuntimeError as e:
                status.set(str(e))
        cleared.clear()
        for fid, (kind, w) in widgets.items():
            if kind == "secret":
                w.delete(0, "end")
        status.set("저장했어요" + (f" (비밀 항목: {', '.join(changed)})" if changed else "")
                   + " — 실행 중인 CharlieDeck 에 곧 적용돼요")
        if close:
            root.destroy()

    ttk.Button(foot, text="저장 후 닫기", command=lambda: save(True)).pack(side="right")
    ttk.Button(foot, text="저장", command=lambda: save(False)).pack(side="right", padx=8)
    ttk.Button(foot, text="닫기", command=root.destroy).pack(side="right")

    def on_close() -> None:
        if messagebox.askyesno("CharlieDeck 설정", "저장하지 않은 내용은 사라져요. 닫을까요?"):
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    lock.close()
    return 0


__all__ = ["FIELDS", "KEYS", "apply_form", "initial_values", "run", "CLEAR"]
