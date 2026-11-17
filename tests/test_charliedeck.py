"""CharlieDeck 테스트 (D200 · hidapi · 화면 · 네트워크 없이 돈다)."""

import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path, PureWindowsPath
from unittest import mock

os.environ.setdefault("CHARLIEDECK_SECRETS", "memory")
_TMP = tempfile.mkdtemp(prefix="charliedeck-test-")
os.environ["CHARLIEDECK_HOME"] = str(Path(_TMP) / "home")
os.environ["CHARLIEDECK_CONFIG_DIR"] = str(Path(_TMP) / "cfg")

from PIL import Image  # noqa: E402

from charliedeck import render, settings, setup_ui, startup  # noqa: E402
from charliedeck.__main__ import Runner, snapshot  # noqa: E402
from charliedeck.app import BRIGHTNESS_STEPS, DeckApp  # noqa: E402
from charliedeck.backends import HeadlessBackend  # noqa: E402
from charliedeck.browser import DEFAULT_APPROVAL_PATTERNS, compile_patterns, extract_count  # noqa: E402
from charliedeck.live import LiveProvider, Pop3Mail, TodoistClient, parse_ical, parse_todoist  # noqa: E402
from charliedeck.model import UNDO_SEC, DemoProvider  # noqa: E402
from charliedeck.pages import PAGES, build  # noqa: E402
from charliedeck.vault import MemoryBackend, Vault, mask  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 17, 10, 40)  # 화요일


def size(png: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(png)).size


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def memory_vault(**values) -> Vault:
    v = Vault(MemoryBackend(), stamp=Path(_TMP) / "stamp")
    for k, val in values.items():
        v.set(k.replace("_", ".", 1), val)
    return v


def demo_app(cfg: dict | None = None):
    mono = FakeClock()
    opened: list[str] = []
    s = settings.from_dict(cfg or {})
    app = DeckApp(s, DemoProvider(monotonic=mono), opener=lambda u: opened.append(u) or True, clock=lambda: NOW,
                  monotonic=mono)
    return app, mono, opened


class FakeBrowser:
    def __init__(self) -> None:
        self.calls: list = []
        self.chrome_path = ""

    def read_count(self, url, patterns, mode="headless", **kw):
        from charliedeck.browser import ReadResult

        self.calls.append(("read", url))
        return ReadResult("ok", 7, "결재 대기 7")

    def open_visible(self, url):
        self.calls.append(("open", url))

    def ecount_login(self, url, c, u, p):
        self.calls.append(("ecount", url, c, u, bool(p)))
        return True, "이카운트에 접속했어요"


# ---------- 화면 ----------
class LayoutTest(unittest.TestCase):
    def test_five_pages(self):
        self.assertEqual([p for p, _ in PAGES], ["home", "todo", "schedule", "hiworks", "links"])

    def test_every_page_renders_13_keys_and_wide(self):
        app, _, _ = demo_app()
        for i in range(len(PAGES)):
            app.go(i)
            keys, wide = app.frame(NOW)
            self.assertEqual(sorted(keys), list(range(13)), PAGES[i])
            self.assertTrue(all(size(p) == (196, 196) for p in keys.values()))
            self.assertEqual(size(wide), (458, 196))

    def test_home_has_requested_five_and_todos(self):
        app, _, _ = demo_app()
        keys = app.keys(NOW)
        self.assertEqual([k.kind for k in keys[:5]], ["mail", "approval", "calendar", "ecount", "goto"])
        self.assertEqual(keys[0].value, "12")
        self.assertEqual(keys[1].value, "4")
        self.assertEqual(keys[2].value, "거래처 미팅")
        self.assertTrue(all(k.kind == "todo" for k in keys[5:9]))
        self.assertEqual(keys[9].kind, "goto")  # 6개 중 5칸: 마지막은 '+2개'
        self.assertEqual(keys[9].value, "+2개")

    def test_setup_needed_screen_shows_setup_not_numbers(self):
        p = LiveProvider(settings.Settings(), memory_vault(), browser=FakeBrowser(), start=False)
        for src in ("mail", "approval", "calendar", "todoist"):
            getattr(p, f"_poll_{src}")()
        keys = build(0, p.get(NOW), NOW, settings.Settings())
        self.assertEqual([k.value for k in keys[:5]], ["설정"] * 5)
        self.assertTrue(all(k.kind == "blank" for k in keys[5:]))  # 할 일 설정 안내가 두 번 나오지 않음


class NavigationTest(unittest.TestCase):
    def test_prev_next_home(self):
        app, _, _ = demo_app()
        self.assertEqual(app.tap(12), "page:todo")
        self.assertEqual(app.tap(12), "page:schedule")
        self.assertEqual(app.tap(11), "page:home")
        self.assertEqual(app.tap(10), "page:links")
        self.assertEqual(app.tap(12), "page:home")
        self.assertEqual(app.tap(11), "refresh")

    def test_long_home_brightness(self):
        app, mono, _ = demo_app()
        seen = []
        app.on_brightness = seen.append
        app.press(11)
        mono.t += 1.0
        self.assertEqual(app.release(11), "brightness:100")
        app.tap(11, long=True)
        self.assertEqual(app.brightness, BRIGHTNESS_STEPS[0])

    def test_wide_short_upcoming_long_setup(self):
        app, _, _ = demo_app()
        opened = []
        app.on_setup = lambda: opened.append(1)
        self.assertEqual(app.tap(13), "wide:upcoming")
        self.assertIn("11:00 거래처 미팅", app.toast.lines)
        self.assertEqual(app.tap(13, long=True), "setup")
        self.assertEqual(opened, [1])

    def test_summary_goes_to_todo_page(self):
        app, _, _ = demo_app()
        self.assertEqual(app.tap(4), "page:todo")


class TodoTest(unittest.TestCase):
    def test_complete_undo_and_commit(self):
        app, mono, _ = demo_app()
        committed = []
        app.provider.commit_done = committed.append
        self.assertEqual(app.tap(5), "todo-done:d1")
        self.assertTrue(app.keys(NOW)[5].todo.done)
        self.assertEqual(app.tap(5), "todo-undo:d1")  # 8초 안에 다시 누르면 취소
        self.assertFalse(app.keys(NOW)[5].todo.done)
        app.tap(5)
        mono.t += UNDO_SEC + 0.1
        app.provider.expire()
        self.assertEqual(committed, ["d1"])
        self.assertNotIn("d1", [k.todo.id for k in app.keys(NOW) if k.todo])  # 목록에서 빠짐

    def test_todoist_parse_sort_and_due_text(self):
        items = [
            {"id": "1", "content": "보통", "priority": 1, "due": {"date": "2026-11-17"}},
            {"id": "2", "content": "급함", "priority": 4, "due": {"date": "2026-11-17T14:00:00"}},
            {"id": "3", "content": "지연", "priority": 1, "due": {"date": "2026-11-15"}},
        ]
        todos = parse_todoist(items, NOW)
        self.assertEqual([t.id for t in todos], ["3", "2", "1"])
        self.assertEqual(todos[0].due, "지연 11/15")
        self.assertTrue(todos[0].overdue)
        self.assertEqual(todos[1].due, "14:00")
        self.assertEqual(todos[2].due, "오늘")

    def test_todoist_client_paginates_and_closes(self):
        calls = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req):
            calls.append((req.get_method(), req.full_url, req.headers.get("Authorization")))
            if "/close" in req.full_url:
                return Resp(b"")
            if "cursor=" in req.full_url:
                return Resp(json.dumps({"results": [{"id": "b"}], "next_cursor": None}).encode())
            return Resp(json.dumps({"results": [{"id": "a"}], "next_cursor": "c1"}).encode())

        c = TodoistClient("tok-123456", opener)
        self.assertEqual([t["id"] for t in c.tasks("today | overdue")], ["a", "b"])
        c.close("a")
        self.assertIn("/api/v1/tasks/filter?query=today", calls[0][1])
        self.assertEqual(calls[-1][:2], ("POST", "https://api.todoist.com/api/v1/tasks/a/close"))
        self.assertEqual(calls[0][2], "Bearer tok-123456")


class CalendarTest(unittest.TestCase):
    ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:1
DTSTART:20261117T020000Z
DTEND:20261117T030000Z
SUMMARY:UTC meeting
LOCATION:Room
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART;VALUE=DATE:20261117
DTEND;VALUE=DATE:20261118
SUMMARY:All day
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART;TZID=Asia/Seoul:20261110T090000
DTEND;TZID=Asia/Seoul:20261110T093000
RRULE:FREQ=WEEKLY
SUMMARY:Weekly standup
END:VEVENT
BEGIN:VEVENT
UID:4
DTSTART:20261117T050000Z
DTEND:20261117T060000Z
STATUS:CANCELLED
SUMMARY:Cancelled
END:VEVENT
END:VCALENDAR
"""

    def test_parse_recurring_all_day_cancelled(self):
        events = parse_ical(self.ICS, date(2026, 11, 16), date(2026, 11, 25))
        titles = [e.title for e in events]
        self.assertIn("Weekly standup", titles)  # 반복 일정 펼침
        self.assertIn("All day", titles)
        self.assertNotIn("Cancelled", titles)
        allday = next(e for e in events if e.title == "All day")
        self.assertTrue(allday.all_day)
        standup = next(e for e in events if e.title == "Weekly standup")
        self.assertEqual(standup.start.date(), date(2026, 11, 17))


class MailTest(unittest.TestCase):
    class FakePOP:
        def __init__(self, uids):
            self.uids = uids
            self.calls = []

        def user(self, u):
            self.calls.append("user")

        def pass_(self, p):
            self.calls.append("pass")

        def uidl(self):
            return b"+OK", [f"{i + 1} {u}".encode() for i, u in enumerate(self.uids)], 0

        def top(self, n, lines):
            subject = "=?utf-8?b?" + __import__("base64").b64encode(f"제목 {n}".encode()).decode() + "?="
            return b"+OK", [b"From: Sales <sales@example.com>", f"Subject: {subject}".encode()], 0

        def quit(self):
            self.calls.append("quit")

        def dele(self, n):  # 절대 불리면 안 됨
            raise AssertionError("POP3 메일을 지우면 안 돼요")

    def test_new_count_against_baseline_and_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            pop = self.FakePOP(["a", "b"])
            box = Pop3Mail("h", 995, "user", "pw", seen_path=Path(tmp) / "seen.json", factory=lambda: pop)
            self.assertEqual(box.check(), (0, []))  # 처음: 지금 있는 메일은 확인한 것으로
            pop.uids = ["a", "b", "c", "d"]
            n, recent = box.check()
            self.assertEqual(n, 2)
            self.assertEqual(recent[0], ("Sales", "제목 4"))
            box.ack()
            self.assertEqual(box.check()[0], 0)
            raw = (Path(tmp) / "seen.json").read_text(encoding="utf-8")
            self.assertNotIn('"a"', raw)  # UIDL 원문 대신 해시만 저장


class ApprovalPatternTest(unittest.TestCase):
    def test_default_patterns(self):
        pats = compile_patterns("", DEFAULT_APPROVAL_PATTERNS)
        self.assertEqual(extract_count("전자결재\n결재 대기 문서 12\n진행 3", pats)[0], 12)
        self.assertEqual(extract_count("결재대기(5)", pats)[0], 5)
        self.assertIsNone(extract_count("결재함이 비어 있어요", pats)[0])

    def test_custom_pattern_first_and_invalid_ignored(self):
        pats = compile_patterns("미처리\\s*(\\d+)\n[잘못된", DEFAULT_APPROVAL_PATTERNS)
        self.assertEqual(extract_count("미처리 9 결재 대기 2", pats)[0], 9)


class LiveProviderTest(unittest.TestCase):
    def test_approval_via_browser_and_ecount_login(self):
        b = FakeBrowser()
        s = settings.from_dict({"hiworks": {"approval_url": "https://office.example-hiworks.com/approval"}})
        v = memory_vault(ecount_company="123456", ecount_user="me", ecount_password="pw-secret")
        p = LiveProvider(s, v, browser=b, start=False)
        p._poll_approval()
        snap = p.get(NOW)
        self.assertEqual(snap.approval_wait, 7)
        self.assertEqual(snap.state("approval").status, "ok")
        self.assertEqual(snap.state("ecount").status, "ok")
        done = []
        p.ecount_login(lambda ok, msg: done.append((ok, msg)))
        p.pool.shutdown(wait=True)
        self.assertEqual(done, [(True, "이카운트에 접속했어요")])
        self.assertEqual(b.calls[-1], ("ecount", "https://login.ecount.com/", "123456", "me", True))

    def test_errors_are_masked(self):
        v = memory_vault(todoist_token="super-secret-token")
        p = LiveProvider(settings.Settings(), v, browser=FakeBrowser(), start=False)
        p._set("todoist", "error", "bad token super-secret-token")
        self.assertNotIn("super-secret-token", p.get(NOW).state("todoist").message)


# ---------- 설정 · 보안 ----------
class SettingsTest(unittest.TestCase):
    def test_defaults_have_no_company_urls(self):
        s = settings.Settings()
        self.assertEqual(s.section("hiworks")["mail_url"], "")
        self.assertEqual(s.section("hiworks")["approval_url"], "")
        self.assertEqual(s.section("hiworks")["pop3_host"], "pop3s.hiworks.com")

    def test_rejects_secret_keys_and_bad_urls(self):
        s = settings.from_dict({
            "hiworks": {"password": "x", "approval_url": "javascript:alert(1)"},
            "links": [{"name": "a", "url": "https://u:p@example.com"}, {"name": "ok", "url": "https://example.com"}],
            "todoist": {"token": "t"},
        })
        self.assertNotIn("password", s.section("hiworks"))
        self.assertEqual(s.section("hiworks")["approval_url"], "")
        self.assertEqual(s.links(), [{"name": "ok", "url": "https://example.com"}])
        self.assertTrue(any("hiworks.password" in w for w in s.warnings))

    def test_save_never_writes_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = settings.save({"brightness": 50, "api_key": "zzz", "hiworks": {"password": "pw"}},
                                 Path(tmp) / "c.json")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("zzz", raw)
            self.assertNotIn('"pw"', raw)
            self.assertEqual(settings.load(path).brightness, 50)

    def test_links_text_roundtrip(self):
        items = settings.parse_links("게시판 | https://a.example.com\n# 주석\nhttps://b.example.com\n")
        self.assertEqual(items, [{"name": "게시판", "url": "https://a.example.com"},
                                 {"name": "링크", "url": "https://b.example.com"}])
        self.assertEqual(settings.parse_links(settings.format_links(items)), items)

    def test_work_url(self):
        self.assertTrue(settings.is_work_url("https://office.hiworks.com/x"))
        self.assertTrue(settings.is_work_url("https://loginbb.ecount.com/"))
        self.assertFalse(settings.is_work_url("https://www.genspark.ai/"))


class SetupFormTest(unittest.TestCase):
    def test_secret_fields_blank_keep_clear_and_set(self):
        v = memory_vault(todoist_token="old-token")
        init = setup_ui.initial_values(settings.Settings().data, v)
        self.assertEqual(init["vault:todoist.token"], "")  # 저장된 값은 다시 보여주지 않음
        data, changed = setup_ui.apply_form({"vault:todoist.token": ""}, settings.Settings().data, v)
        self.assertEqual(v.get("todoist.token"), "old-token")  # 빈칸 = 그대로
        self.assertEqual(changed, [])
        setup_ui.apply_form({"vault:todoist.token": "new-token"}, data, v)
        self.assertEqual(v.get("todoist.token"), "new-token")
        setup_ui.apply_form({"vault:todoist.token": setup_ui.CLEAR}, data, v)
        self.assertEqual(v.get("todoist.token"), "")

    def test_config_fields_and_links_go_to_json(self):
        v = memory_vault()
        data, _ = setup_ui.apply_form({"cfg:hiworks.approval_url": " https://office.example.com/appr ",
                                       "cfg:hiworks.approval_poll_min": "3", "cfg:hiworks.approval_headless": False,
                                       "cfg:links": "게시판 | https://office.example.com/board",
                                       "vault:hiworks.user": "someone"}, settings.Settings().data, v)
        self.assertEqual(data["hiworks"]["approval_url"], "https://office.example.com/appr")
        self.assertEqual(data["hiworks"]["approval_poll_min"], 3)
        self.assertFalse(data["hiworks"]["approval_headless"])
        self.assertEqual(data["links"], [{"name": "게시판", "url": "https://office.example.com/board"}])
        self.assertEqual(v.get("hiworks.user"), "someone")
        self.assertNotIn("someone", json.dumps(data, ensure_ascii=False))

    def test_every_vault_field_is_known_key(self):
        from charliedeck.vault import KEYS

        for _, fid, *_ in setup_ui.FIELDS:
            if fid.startswith("vault:"):
                self.assertIn(fid[6:], KEYS)


class VaultTest(unittest.TestCase):
    def test_set_get_delete_and_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            v = Vault(MemoryBackend(), stamp=Path(tmp) / "s")
            v.set("todoist.token", "abc")
            self.assertTrue(v.has("todoist.token"))
            self.assertGreater(v.stamp_mtime(), 0)
            v.delete("todoist.token")
            self.assertFalse(v.has("todoist.token"))
            with self.assertRaises(KeyError):
                v.set("unknown.key", "x")

    def test_mask(self):
        self.assertEqual(mask("token=abcd1234 fail", ["abcd1234"]), "token=*** fail")


class SourceHygieneTest(unittest.TestCase):
    def files(self):
        return [p for p in ROOT.rglob("*") if p.suffix in (".py", ".json", ".bat", ".md", ".yml")
                and ".git" not in p.parts and "tests" not in p.parts]

    def test_no_emails_or_keys_in_repo(self):
        email = re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.)[A-Za-z0-9-]+\.(com|net|co\.kr|kr|io|ai)\b")
        key = re.compile(r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|[0-9a-f]{40})")
        for f in self.files():
            text = f.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(email.search(text), f)
            self.assertIsNone(key.search(text), f)

    def test_no_macos_code_left(self):
        pat = re.compile(r"osascript|afplay|launchctl|launchd|/System/Library|AppleLanguages|sys\.platform == .darwin.")
        for f in ROOT.rglob("*.py"):
            if ".git" in f.parts or f.name == Path(__file__).name:
                continue
            self.assertIsNone(pat.search(f.read_text(encoding="utf-8")), f)
        for removed in ("dashboard.py", "scripts/install-launchd.sh", "hooks", "games.py", "i18n.py"):
            self.assertFalse((ROOT / removed).exists(), removed)


# ---------- Windows ----------
class WindowsTest(unittest.TestCase):
    ENV = {"APPDATA": r"C:\Users\tester\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\tester\AppData\Local",
           "CHARLIEDECK_FORCE_WINDOWS_PATHS": "1"}

    def test_paths(self):
        from platform_util import config_dir, data_dir

        self.assertEqual(str(PureWindowsPath(data_dir(self.ENV))), r"C:\Users\tester\AppData\Local\CharlieDeck")
        self.assertEqual(str(PureWindowsPath(config_dir(self.ENV))), r"C:\Users\tester\AppData\Roaming\CharlieDeck")
        self.assertEqual(str(PureWindowsPath(startup.entry_path(self.ENV))),
                         r"C:\Users\tester\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\CharlieDeck.vbs")

    def test_vbs_for_python_and_exe(self):
        vbs = startup.build_vbs(r"C:\Users\홍 길동\deck\.venv\Scripts\pythonw.exe", r"C:\Users\홍 길동\deck", ["--demo"])
        self.assertIn('sh.Run """C:\\Users\\홍 길동\\deck\\.venv\\Scripts\\pythonw.exe"" -m charliedeck --demo", 0, False',
                      vbs)
        exe = startup.build_vbs(r"C:\Program Files\CharlieDeck\CharlieDeck.exe", r"C:\Program Files\CharlieDeck", [],
                                exe=True)
        self.assertIn('sh.Run """C:\\Program Files\\CharlieDeck\\CharlieDeck.exe""", 0, False', exe)
        self.assertNotIn("-m charliedeck", exe)

    def test_install_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"APPDATA": tmp}
            path = startup.install([], python=r"C:\Python311\python.exe", env=env)
            self.assertIn("-m charliedeck", path.read_text(encoding="utf-16"))
            self.assertTrue(startup.uninstall(env))

    def test_batch_files_crlf(self):
        bats = list((ROOT / "scripts" / "windows").glob("*.bat"))
        self.assertTrue(bats)
        for f in bats:
            data = f.read_bytes()
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), f.name)


class DeviceTest(unittest.TestCase):
    def test_pick_interface(self):
        from d200 import pick_interface

        win = [{"path": b"col01", "interface_number": -1, "usage_page": 0x0C},
               {"path": b"col02", "interface_number": -1, "usage_page": 0xFF60}]
        self.assertEqual(pick_interface(win)["path"], b"col02")
        self.assertIsNone(pick_interface([]))

    def test_zip_payload_firmware_safe(self):
        from d200 import Button, _payload_safe, build_zip

        app, _, _ = demo_app()
        keys, wide = app.frame(NOW)
        self.assertTrue(_payload_safe(build_zip({i: Button(image=p) for i, p in keys.items()}, wide)))

    def test_imports_without_hidapi(self):
        import importlib

        import d200

        with mock.patch.dict(sys.modules, {"hid": None}):
            importlib.reload(d200)
            with self.assertRaises(RuntimeError):
                d200.D200()
        importlib.reload(d200)


class RunnerTest(unittest.TestCase):
    def test_redraw_only_on_change_and_reload_on_settings_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "c.json"
            app, _, _ = demo_app()
            backend = HeadlessBackend()
            r = Runner(app, backend, vault=None, config=str(cfg))
            r.tick()
            r.tick()
            self.assertEqual(backend.frames, 1)
            backend.on_press(12)
            backend.on_release(12)
            r.tick()
            self.assertEqual(backend.frames, 2)
            settings.save({"brightness": 20}, cfg)
            r._watch_at = 0
            r.tick()
            self.assertEqual(app.brightness, 20)
            self.assertEqual(backend.brightness, 20)  # 새 밝기가 장치에도 전달됨
            self.assertEqual(app.toast.title, "설정")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_demo_and_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            names = [f.name for f in snapshot(tmp, NOW)]
            self.assertEqual(len(names), 11)
            self.assertIn("demo-06-todo-done.png", names)
            self.assertIn("setup-01-home.png", names)
            summary = json.loads((Path(tmp) / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(set(summary["modes"]), {"demo", "setup"})


class RenderTest(unittest.TestCase):
    def test_tiles(self):
        self.assertEqual(size(render.todo_tile("10:00", "견적서 회신 보내기", render.RED, overdue=True)), (196, 196))
        self.assertEqual(size(render.todo_tile("완료", "아주 긴 할 일 " * 6, done=True)), (196, 196))
        self.assertEqual(size(render.key_tile("하이웍스", "로그인", "눌러서", warn=True)), (196, 196))
        wide = render.wide_panel(NOW, ("a", "b"), 0, calendar=("off", ""), counts=(1, None, 3))
        self.assertEqual(size(wide), (458, 196))

    def test_fonts_prefer_windows_before_linux(self):
        import fonts

        order = [str(p) for p, _ in fonts._KR["regular"]]
        win = next(i for i, p in enumerate(order) if p.lower().endswith("malgun.ttf"))
        nanum = next(i for i, p in enumerate(order) if "Nanum" in p)
        self.assertLess(win, nanum)


if __name__ == "__main__":
    unittest.main()
