from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import os
import random
import re
import sqlite3
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET
from event_store import EventStore

# Python 的 mimetypes 在 Windows 上不认字体后缀，不注册会以
# application/octet-stream 返回 woff2，Chromium 会拒绝加载本地字体。
for _ext, _mime in (
    (".woff2", "font/woff2"),
    (".woff", "font/woff"),
    (".ttf", "font/ttf"),
    (".otf", "font/otf"),
):
    mimetypes.add_type(_mime, _ext)

ROOT = Path(__file__).resolve().parent
DIST = Path(os.environ.get("LUCKY_DRAW_DIST_DIR", ROOT / "dist"))
DATA = Path(os.environ.get("LUCKY_DRAW_DATA_DIR", ROOT / "data"))
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "lucky-draw.sqlite"
DB_LOCK = threading.RLock()
ADMIN_PASSWORD = os.environ.get("LUCKY_DRAW_ADMIN_PASSWORD", "admin123")
AUTH_TOKENS: set[str] = set()
EVENTS = EventStore(DATA)
UPDATE_INSTALLING = False

# 模板不内置真实嘉宾。现场从后台导入名单或手动添加即可。
SEED_GUESTS: list[tuple[str, str, str, str, int]] = []


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    # Keep a blocked request from freezing the UI for ten seconds when an old
    # process or an external SQLite reader still has the file open.
    conn = sqlite3.connect(DB_PATH, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 2000")
    # WAL 下 synchronous=NORMAL 只能防进程崩溃，防不了断电/异常关机：
    # 那种情况下主库可能与 WAL 混合，出现「索引里有、表里没有」这类不一致。
    conn.execute("PRAGMA synchronous = FULL")
    return conn


DB_HEALTH: dict = {"ok": True, "repaired": False, "problems": [], "backup": None, "checked_at": None}


def integrity_problems(conn) -> list[str]:
    """返回 PRAGMA integrity_check 的问题清单（空列表 = 正常）。

    这里必须用 integrity_check 而不是 quick_check —— 后者恰恰不校验
    「索引内容与表内容是否一致」，而那种不一致正是「列表里看得到这位嘉宾，
    一按 guest_id 查就说找不到」的成因。
    """
    try:
        rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.DatabaseError as exc:
        return [f"integrity_check 执行失败：{exc}"]
    if len(rows) == 1 and rows[0].strip().lower() == "ok":
        return []
    return rows


def backup_database(conn, tag: str) -> str | None:
    """用 SQLite 官方备份 API 存一份一致性快照（能正确处理 WAL，直接拷文件不行）。"""
    try:
        folder = os.path.join(os.path.dirname(DB_PATH) or ".", "backup")
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(folder, f"lucky-draw-{stamp}-{tag}.sqlite")
        target = sqlite3.connect(dest)
        try:
            conn.backup(target)
        finally:
            target.close()
        return dest
    except Exception as exc:  # 备份失败不该阻塞修复流程
        print(f"[db] 备份失败：{exc}", flush=True)
        return None


def repair_indexes(conn, tag: str) -> bool:
    """REINDEX 按表数据重建全部索引：业务数据不动，只重建索引结构。"""
    try:
        conn.execute("REINDEX")
        conn.commit()
    except sqlite3.DatabaseError as exc:
        print(f"[db] REINDEX 失败：{exc}", flush=True)
        return False
    problems = integrity_problems(conn)
    DB_HEALTH.update(ok=not problems, repaired=True, problems=problems)
    return not problems


def verify_database(conn, tag: str) -> list[str]:
    """自检；发现问题先备份再修复，结果写进 DB_HEALTH 供界面提示。"""
    problems = integrity_problems(conn)
    DB_HEALTH["checked_at"] = now()
    DB_HEALTH["problems"] = problems
    if not problems:
        DB_HEALTH["ok"] = True
        return []
    print(f"[db] integrity_check 发现 {len(problems)} 个问题（{tag}），先做备份", flush=True)
    for item in problems[:5]:
        print(f"[db]   - {item}", flush=True)
    DB_HEALTH["backup"] = backup_database(conn, tag)
    ok = repair_indexes(conn, tag)
    print("[db] 已 REINDEX 修复" if ok else "[db] REINDEX 后仍有问题，请联系开发", flush=True)
    return problems


def find_guest(conn, guest_id: str):
    """按 guest_id 取嘉宾行。

    索引一旦与表不一致，`WHERE guest_id=?` 会走索引而查不到这一行，
    但 `SELECT *` 的全表扫描仍然能看到它 —— 表现为「列表里明明有，
    删除/编辑却说找不到这位嘉宾」。`+guest_id` 会阻止查询计划器使用索引，
    因此可以做一次全表扫描兜底，并顺手把索引修好。
    """
    row = conn.execute("SELECT * FROM guests WHERE guest_id=?", (guest_id,)).fetchone()
    if row is not None:
        return row
    row = conn.execute("SELECT * FROM guests WHERE +guest_id=?", (guest_id,)).fetchone()
    if row is not None:
        print("[db] 命中「索引查不到、表里有」的嘉宾，正在修复索引", flush=True)
        repair_indexes(conn, "auto")
    return row


def db_health_payload() -> dict:
    return {
        "ok": DB_HEALTH["ok"],
        "repaired": DB_HEALTH["repaired"],
        "problems": DB_HEALTH["problems"][:5],
        "backup": DB_HEALTH["backup"],
        "checked_at": DB_HEALTH["checked_at"],
    }


def init_db() -> None:
    with DB_LOCK, connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              event_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guests (
              guest_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              company TEXT NOT NULL DEFAULT '',
              email TEXT NOT NULL DEFAULT '',
              session_id INTEGER NOT NULL REFERENCES sessions(id),
              checked_in INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'eligible',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              guest_id TEXT NOT NULL REFERENCES guests(guest_id),
              session_id INTEGER NOT NULL REFERENCES sessions(id),
              action TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prizes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id INTEGER NOT NULL REFERENCES sessions(id),
              name TEXT NOT NULL,
              quantity INTEGER NOT NULL DEFAULT 1,
              used_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_guests_session_status
              ON guests(session_id, status, checked_in);
            CREATE INDEX IF NOT EXISTS idx_records_session
              ON records(session_id, created_at);
            """
        )
        record_columns = {row[1] for row in conn.execute("PRAGMA table_info(records)").fetchall()}
        if "prize_name" not in record_columns:
            conn.execute("ALTER TABLE records ADD COLUMN prize_name TEXT NOT NULL DEFAULT ''")
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "pending_prize_id" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN pending_prize_id INTEGER")
        if "draw_state" not in session_columns:
            # rolling：大屏持续滚动，paused：后台已按下暂停并揭晓，idle：没有进行中的抽奖。
            conn.execute("ALTER TABLE sessions ADD COLUMN draw_state TEXT NOT NULL DEFAULT 'idle'")
            # 升级旧数据库时，原来已经进入 pending 的嘉宾此前都已在大屏显示，按揭晓处理。
            conn.execute(
                "UPDATE sessions SET draw_state='paused' WHERE EXISTS "
                "(SELECT 1 FROM guests WHERE guests.session_id=sessions.id AND guests.status='pending')"
            )
        conn.executemany(
            "INSERT OR IGNORE INTO sessions(id, name, event_date) VALUES (?, ?, ?)",
            [(1, "Session 01", "待设置"), (2, "Session 02", "待设置")],
        )
        if conn.execute("SELECT COUNT(*) FROM guests").fetchone()[0] == 0:
            stamp = now()
            conn.executemany(
                """INSERT INTO guests(guest_id,name,company,email,session_id,created_at,updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(*g, stamp, stamp) for g in SEED_GUESTS],
            )
        if conn.execute("SELECT COUNT(*) FROM prizes").fetchone()[0] == 0:
            stamp = now()
            conn.executemany(
                "INSERT INTO prizes(session_id,name,quantity,created_at) VALUES (?, ?, ?, ?)",
                [(1, "Grand Prize", 1, stamp), (1, "Lucky Prize", 2, stamp), (2, "Grand Prize", 1, stamp), (2, "Lucky Prize", 2, stamp)],
            )
        conn.commit()
        # 启动自检：索引/表不一致时自动修复（修复前会先备份快照）。
        verify_database(conn, "startup")
        # 把 WAL 收干净，避免长期堆积导致异常关机时更容易出现主库与 WAL 混合。
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.DatabaseError:
            pass


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def guest_payload(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["checked"] = bool(data.pop("checked_in"))
    return data


DEFAULT_SETTINGS = {"screen_controls": "1", "screen_actions": "1"}


def get_settings(conn) -> dict:
    merged = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM app_settings").fetchall():
        merged[row["key"]] = row["value"]
    return merged


def settings_payload(conn) -> dict:
    raw = get_settings(conn)
    return {key: (1 if str(raw.get(key, DEFAULT_SETTINGS[key])) == "1" else 0) for key in DEFAULT_SETTINGS}


def state_payload(session_id: int) -> dict:
    with DB_LOCK, connect() as conn:
        session = row_dict(conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())
        rows = conn.execute("SELECT * FROM guests WHERE session_id=? ORDER BY name", (session_id,)).fetchall()
        records = conn.execute(
            """SELECT r.*, g.name, g.company, g.email, g.guest_id
               FROM records r JOIN guests g ON g.guest_id=r.guest_id
               WHERE r.session_id=? ORDER BY r.id DESC LIMIT 100""",
            (session_id,),
        ).fetchall()
        prizes = conn.execute("SELECT * FROM prizes WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        # A draw can now hold several guests in the pending state.  Keep the
        # legacy `pending` field below so an older screen still has a useful
        # first winner to display, and expose the complete group separately.
        pending_rows = conn.execute(
            "SELECT * FROM guests WHERE session_id=? AND status='pending' ORDER BY name, guest_id", (session_id,)
        ).fetchall()
        pending_prize = conn.execute("SELECT * FROM prizes WHERE id=?", (session.get("pending_prize_id"),)).fetchone() if session and session.get("pending_prize_id") else None
        return {
            "event": EVENTS.public(),
            "session": session,
            "guests": [guest_payload(row) for row in rows],
            "pending": guest_payload(pending_rows[0]) if pending_rows else None,
            "pending_guests": [guest_payload(row) for row in pending_rows],
            "pending_prize": row_dict(pending_prize),
            "records": [dict(r) for r in records],
            "prizes": [dict(r) for r in prizes],
            "settings": settings_payload(conn),
            "db": db_health_payload(),
        }


def parse_json(handler: SimpleHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw.decode("utf-8"))


def normalize_guest(item: dict, session_id: int) -> tuple[str, str, str, str, int]:
    def field(*names):
        aliases = {str(name).strip().lower().replace(" ", "_").replace("-", "_") for name in names}
        for key, value in item.items():
            normalized = str(key).strip().lower().replace(" ", "_").replace("-", "_")
            if normalized in aliases and value is not None:
                return value
        return ""

    guest_id = str(field("guest_id", "guestId", "Guest ID", "id")).strip()
    name = str(field("name", "guest_name", "Guest Name")).strip()
    company = str(field("company", "company_name", "Company Name")).strip()
    email = str(field("email", "Email")).strip().casefold()
    raw_session = field("session_id", "session", "Session") or session_id
    try:
        raw_session = int(raw_session)
    except (TypeError, ValueError):
        raw_session = session_id
    if raw_session not in (1, 2):
        raw_session = session_id
    if not guest_id:
        guest_id = f"G-{int(time.time() * 1000)}-{random.randint(100, 999)}"
    if not name:
        raise ValueError("缺少嘉宾姓名")
    return guest_id, name, company, email, raw_session


def is_example_guest(item: dict) -> bool:
    """Ignore the clearly marked sample row shipped with the template."""
    marker = " ".join(str(value or "").strip().casefold() for value in item.values())
    return any(token in marker for token in ("example - do not import", "do not import", "示例", "不要导入"))


def ensure_unique_import_email(conn: sqlite3.Connection, email: str, guest_id: str, seen_emails: set[str]) -> None:
    """Reject duplicate non-empty emails, both in the workbook and existing guest list."""
    email_key = email.strip().casefold()
    if not email_key:
        return
    if email_key in seen_emails:
        raise ValueError("文件内邮箱重复，不能重复导入")
    existing = conn.execute(
        "SELECT guest_id FROM guests WHERE lower(email)=? LIMIT 1", (email_key,)
    ).fetchone()
    if existing and existing["guest_id"] != guest_id:
        raise ValueError("邮箱已存在，不能重复导入")
    seen_emails.add(email_key)


def parse_csv(raw: bytes) -> list[dict]:
    text = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))


def parse_xlsx(raw: bytes) -> list[dict]:
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    rel_ns = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in item.findall(".//a:t", ns)) for item in root.findall("a:si", ns)]
        sheet_path = "xl/worksheets/sheet1.xml"
        if sheet_path not in archive.namelist():
            raise ValueError("Excel 文件中没有找到第一个工作表")
        root = ET.fromstring(archive.read(sheet_path))
        rows = []
        for row in root.findall(".//a:row", ns):
            values = []
            for cell in row.findall("a:c", ns):
                ref = cell.get("r", "A1")
                col = re.match(r"([A-Z]+)", ref).group(1)
                col_num = 0
                for ch in col:
                    col_num = col_num * 26 + ord(ch) - 64
                while len(values) < col_num:
                    values.append("")
                value = cell.find("a:v", ns)
                value_text = value.text if value is not None else ""
                if cell.get("t") == "s" and value_text:
                    value_text = shared[int(value_text)]
                values[col_num - 1] = value_text
            rows.append(values)
        if not rows:
            return []
        # The downloadable template has a title and instructions above the
        # actual header row. Find the first row containing the required
        # columns instead of assuming the header is row 1.
        header_index = None
        for index, candidate in enumerate(rows):
            normalized = {
                str(value).strip().lower().replace(" ", "_").replace("-", "_")
                for value in candidate if str(value).strip()
            }
            if {"guest_id", "guest_name"}.issubset(normalized):
                header_index = index
                break
        if header_index is None:
            raise ValueError("找不到表头。请确保包含 Guest ID 和 Guest Name 两列")
        headers = [str(v).strip() for v in rows[header_index]]
        return [
            dict(zip(headers, row + [""] * max(0, len(headers) - len(row))))
            for row in rows[header_index + 1:]
            if any(str(value).strip() for value in row)
        ]


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def end_json(self, payload: dict, status=HTTPStatus.OK):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self) -> bool:
        return self.headers.get("X-Admin-Token", "") in AUTH_TOKENS

    def do_GET(self):
        # Switching the active database and API reads/writes share one lock.
        # A request must never cross event boundaries halfway through.
        if self.path.startswith('/api/'):
            with DB_LOCK:
                return self.event_get()
        return super().do_GET()

    def event_get(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/events':
            if not self.authorized():
                return self.end_json({'ok': False, 'error': '请先登录后台'}, 401)
            return self.end_json({'events': EVENTS.list(), 'active': EVENTS.public()})
        if parsed.path == '/api/event-asset':
            query = urllib.parse.parse_qs(parsed.query)
            try:
                kind = query.get('kind', [''])[0]
                if kind not in ('logo', 'background'):
                    raise ValueError('图片不存在')
                value = EVENTS.get(query.get('id', [''])[0]).get(kind, '')
                header, encoded = value.split(',', 1)
                raw = base64.b64decode(encoded)
            except (ValueError, KeyError):
                return self.end_json({'ok': False, 'error': '图片不存在'}, 404)
            self.send_response(200)
            self.send_header('Content-Type', header[5:].split(';')[0])
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return self.legacy_get()

    def legacy_get(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/state":
            query = urllib.parse.parse_qs(parsed.query)
            session_id = int(query.get("session", [1])[0])
            return self.end_json(state_payload(session_id))
        if parsed.path == "/api/health":
            return self.end_json({"ok": True, "database": str(DB_PATH)})
        if parsed.path == "/api/auth":
            # 只回答「这个 token 还有效吗」，让前端启动时能校验 localStorage 里的旧 token
            return self.end_json({"ok": self.authorized()})
        if parsed.path == "/api/export":
            query = urllib.parse.parse_qs(parsed.query)
            session_id = int(query.get("session", [1])[0])
            with DB_LOCK, connect() as conn:
                rows = conn.execute("""SELECT r.created_at, g.guest_id, g.name, g.company, g.email,
                    r.session_id, r.prize_name, r.action, r.note
                    FROM records r JOIN guests g ON g.guest_id=r.guest_id
                    WHERE r.session_id=? ORDER BY r.id""", (session_id,)).fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["Draw Time", "Guest ID", "Guest Name", "Company", "Email", "Session", "Prize", "Result", "Note"])
            for row in rows:
                writer.writerow([row["created_at"], row["guest_id"], row["name"], row["company"], row["email"], row["session_id"], row["prize_name"], row["action"], row["note"]])
            raw = output.getvalue().encode("utf-8-sig")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="winner-records-session-{session_id}.csv"')
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            return
        return super().do_GET()

    def do_POST(self):
        global DB_PATH, UPDATE_INSTALLING
        with DB_LOCK:
            route = urllib.parse.urlparse(self.path).path
            if route == '/api/updates/prepare':
                expected = os.environ.get('LUCKY_DRAW_UPDATE_TOKEN')
                if not expected or self.headers.get('X-Update-Token') != expected:
                    return self.end_json({'ok': False, 'error': '无效的更新请求'}, 403)
                with connect() as conn:
                    if conn.execute("SELECT 1 FROM guests WHERE status='pending'").fetchone():
                        return self.end_json({'ok': False, 'error': '请先确认或取消本轮抽奖，再安装更新'}, 409)
                UPDATE_INSTALLING = True
                return self.end_json({'ok': True})
            if UPDATE_INSTALLING:
                return self.end_json({'ok': False, 'error': '正在准备安装更新，请稍候'}, 409)
            if route in ('/api/login', '/api/logout'):
                return self.legacy_post()
            if not self.authorized():
                return self.end_json({'ok': False, 'error': '请先登录后台'}, 401)
            if self.headers.get('X-Event-Id') != EVENTS.active():
                return self.end_json({'ok': False, 'error': '活动已切换，请刷新后重试'}, 409)
            if route.startswith('/api/events/'):
                try:
                    if int(self.headers.get('Content-Length', '0')) > 12_000_000:
                        raise ValueError('配置文件太大')
                    body = parse_json(self)
                    with connect() as conn:
                        if conn.execute("SELECT 1 FROM guests WHERE status='pending'").fetchone():
                            raise ValueError('请先确认或取消正在进行的抽奖，再修改或切换活动')
                    if route == '/api/events/create':
                        event_id = EVENTS.save(body, create=True)
                        original = DB_PATH
                        try:
                            DB_PATH = EVENTS.database(event_id)
                            init_db()
                        finally:
                            DB_PATH = original
                    elif route == '/api/events/update':
                        if body.get('id') != EVENTS.active():
                            raise ValueError('请先切换到要修改的活动')
                        event_id = EVENTS.save(body)
                    elif route == '/api/events/switch':
                        event_id = str(body.get('id', ''))
                        target = EVENTS.database(event_id)
                        original = DB_PATH
                        try:
                            DB_PATH = target
                            init_db()
                            EVENTS.select(event_id)
                        except Exception:
                            DB_PATH = original
                            raise
                    else:
                        return self.end_json({'ok': False, 'error': 'Not found'}, 404)
                    return self.end_json({'ok': True, 'id': event_id})
                except (ValueError, sqlite3.Error) as exc:
                    return self.end_json({'ok': False, 'error': str(exc)}, 400)
            return self.legacy_post()

    def legacy_post(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/api/login":
                body = parse_json(self)
                if str(body.get("password", "")) != ADMIN_PASSWORD:
                    return self.end_json({"ok": False, "error": "密码不正确"}, HTTPStatus.UNAUTHORIZED)
                token = base64.urlsafe_b64encode(os.urandom(24)).decode("ascii")
                AUTH_TOKENS.add(token)
                return self.end_json({"ok": True, "token": token})
            if not self.authorized():
                return self.end_json({"ok": False, "error": "请先登录后台"}, HTTPStatus.UNAUTHORIZED)
            if parsed.path == "/api/logout":
                AUTH_TOKENS.discard(self.headers.get("X-Admin-Token", ""))
                return self.end_json({"ok": True})
            if parsed.path == "/api/settings":
                body = parse_json(self)
                with DB_LOCK, connect() as conn:
                    for key in DEFAULT_SETTINGS:
                        if key in body:
                            value = "1" if body.get(key) in (1, True, "1", "true", "on") else "0"
                            conn.execute(
                                "INSERT INTO app_settings(key,value) VALUES (?,?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (key, value),
                            )
                    conn.commit()
                    return self.end_json({"ok": True, "settings": settings_payload(conn)})
            if parsed.path == "/api/guest/check":
                body = parse_json(self)
                guest_id = str(body.get("guest_id"))
                checked = bool(body.get("checked"))
                with DB_LOCK, connect() as conn:
                    if not find_guest(conn, guest_id):
                        return self.end_json({"ok": False, "error": "找不到该嘉宾"}, HTTPStatus.NOT_FOUND)
                    conn.execute("UPDATE guests SET checked_in=?, updated_at=? WHERE guest_id=? AND status='eligible'", (int(checked), now(), guest_id))
                    conn.commit()
                return self.end_json({"ok": True})
            if parsed.path == "/api/guest/delete":
                body = parse_json(self)
                guest_id = str(body.get("guest_id"))
                with DB_LOCK, connect() as conn:
                    row = find_guest(conn, guest_id)
                    if not row:
                        return self.end_json({"ok": False, "error": "找不到这位嘉宾"}, HTTPStatus.NOT_FOUND)
                    if row["status"] == "pending":
                        return self.end_json(
                            {"ok": False, "error": "TA 正在等待确认，请先在「抽奖控制」页确认中奖、重抽并排除或暂停抽奖"},
                            HTTPStatus.BAD_REQUEST,
                        )
                    removed_records = conn.execute("DELETE FROM records WHERE guest_id=?", (guest_id,)).rowcount
                    conn.execute("DELETE FROM guests WHERE guest_id=?", (guest_id,))
                    conn.execute("UPDATE sessions SET pending_prize_id=NULL WHERE id=? AND pending_prize_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM guests WHERE session_id=? AND status='pending')", (row["session_id"], row["session_id"]))
                    conn.commit()
                return self.end_json({"ok": True, "deleted_records": removed_records})
            if parsed.path == "/api/guest/status":
                body = parse_json(self)
                guest_id = str(body.get("guest_id"))
                status = str(body.get("status"))
                if status not in {"not_checked", "checked_in", "eligible", "confirmed_winner", "redraw_excluded"}:
                    return self.end_json({"ok": False, "error": "不支持的嘉宾状态"}, HTTPStatus.BAD_REQUEST)
                if status == "not_checked":
                    status, checked_in = "eligible", 0
                elif status == "checked_in":
                    status, checked_in = "eligible", 1
                else:
                    checked_in = 1 if status != "eligible" else None
                with DB_LOCK, connect() as conn:
                    row = find_guest(conn, guest_id)
                    if not row:
                        return self.end_json({"ok": False, "error": "找不到该嘉宾"}, HTTPStatus.NOT_FOUND)
                    conn.execute("UPDATE guests SET status=?, checked_in=COALESCE(?, checked_in), updated_at=? WHERE guest_id=?", (status, checked_in, now(), guest_id))
                    if status in {"confirmed_winner", "redraw_excluded"}:
                        conn.execute("INSERT INTO records(guest_id,session_id,action,note,prize_name,created_at) VALUES (?,?,?,?,?,?)", (guest_id, row["session_id"], "manual_status", "后台手动修改", "", now()))
                    conn.commit()
                return self.end_json({"ok": True})
            if parsed.path == "/api/guest/add":
                body = parse_json(self)
                guest_id, name, company, email, session_id = normalize_guest(body, int(body.get("session_id", 1)))
                with DB_LOCK, connect() as conn:
                    find_guest(conn, guest_id)  # 索引损坏会漏掉重复检测，先探一次并修复
                    conn.execute("""INSERT INTO guests(guest_id,name,company,email,session_id,created_at,updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""", (guest_id, name, company, email, session_id, now(), now()))
                    conn.commit()
                return self.end_json({"ok": True, "guest_id": guest_id})
            if parsed.path == "/api/guest/update":
                body = parse_json(self)
                guest_id = str(body.get("guest_id", "")).strip()
                name = str(body.get("name", "")).strip()
                company = str(body.get("company", "")).strip()
                email = str(body.get("email", "")).strip()
                if not guest_id or not name:
                    return self.end_json({"ok": False, "error": "Guest ID 和嘉宾姓名不能为空"}, HTTPStatus.BAD_REQUEST)
                with DB_LOCK, connect() as conn:
                    updated = conn.execute("UPDATE guests SET name=?, company=?, email=?, updated_at=? WHERE guest_id=?", (name, company, email, now(), guest_id)).rowcount
                    if not updated:
                        return self.end_json({"ok": False, "error": "找不到该嘉宾"}, HTTPStatus.NOT_FOUND)
                    conn.commit()
                return self.end_json({"ok": True})
            if parsed.path in {"/api/import/preview", "/api/import"}:
                body = parse_json(self)
                session_id = int(body.get("session_id", 1))
                preview_only = parsed.path.endswith("/preview")
                if isinstance(body.get("rows"), list):
                    rows = body["rows"]
                else:
                    raw = base64.b64decode(body.get("content", ""))
                    filename = str(body.get("filename", "")).lower()
                    rows = parse_xlsx(raw) if filename.endswith(".xlsx") else parse_csv(raw)
                if preview_only:
                    preview, valid_rows, skipped = [], [], 0
                    with DB_LOCK, connect() as conn:
                        seen_emails: set[str] = set()
                        for index, item in enumerate(rows, start=2):
                            if is_example_guest(item):
                                skipped += 1
                                preview.append({"row": index, "status": "example", "name": str(item.get("Guest Name") or item.get("name") or "示例行")})
                                continue
                            try:
                                guest_id, name, company, email, row_session = normalize_guest(item, session_id)
                                ensure_unique_import_email(conn, email, guest_id, seen_emails)
                                valid = {"guest_id": guest_id, "name": name, "company": company, "email": email, "session_id": row_session}
                                valid_rows.append(valid)
                                preview.append({"row": index, "status": "ready", **valid})
                            except Exception as exc:
                                skipped += 1
                                preview.append({"row": index, "status": "error", "error": str(exc)})
                    return self.end_json({"ok": True, "total": len(rows), "ready": len(valid_rows), "skipped": skipped, "preview": preview, "rows": valid_rows})
                imported, skipped, errors = 0, 0, []
                with DB_LOCK, connect() as conn:
                    seen_emails: set[str] = set()
                    for index, item in enumerate(rows, start=2):
                        try:
                            if is_example_guest(item):
                                skipped += 1
                                continue
                            guest_id, name, company, email, row_session = normalize_guest(item, session_id)
                            ensure_unique_import_email(conn, email, guest_id, seen_emails)
                            conn.execute("""INSERT INTO guests(guest_id,name,company,email,session_id,created_at,updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(guest_id) DO UPDATE SET name=excluded.name, company=excluded.company,
                                  email=excluded.email, session_id=excluded.session_id, updated_at=excluded.updated_at""",
                                (guest_id, name, company, email, row_session, now(), now()))
                            imported += 1
                        except Exception as exc:
                            skipped += 1
                            errors.append(f"第 {index} 行：{exc}")
                    conn.commit()
                    verify_database(conn, "import")
                return self.end_json({"ok": True, "imported": imported, "skipped": skipped, "errors": errors[:10]})
            if parsed.path == "/api/prize/add":
                body = parse_json(self)
                session_id = int(body.get("session_id", 1))
                name = str(body.get("name", "")).strip()
                quantity = max(1, int(body.get("quantity", 1)))
                if not name:
                    return self.end_json({"ok": False, "error": "请输入奖品名称"}, HTTPStatus.BAD_REQUEST)
                with DB_LOCK, connect() as conn:
                    conn.execute("INSERT INTO prizes(session_id,name,quantity,created_at) VALUES (?,?,?,?)", (session_id, name, quantity, now()))
                    conn.commit()
                return self.end_json({"ok": True})
            if parsed.path == "/api/prize/delete":
                body = parse_json(self)
                prize_id = int(body.get("prize_id"))
                with DB_LOCK, connect() as conn:
                    if conn.execute("SELECT 1 FROM sessions WHERE pending_prize_id=?", (prize_id,)).fetchone():
                        return self.end_json({"ok": False, "error": "该奖品正在等待确认，暂时不能删除"}, HTTPStatus.BAD_REQUEST)
                    deleted = conn.execute("DELETE FROM prizes WHERE id=?", (prize_id,)).rowcount
                    if not deleted:
                        return self.end_json({"ok": False, "error": "找不到该奖品"}, HTTPStatus.NOT_FOUND)
                    conn.commit()
                return self.end_json({"ok": True})
            if parsed.path == "/api/prize/update":
                body = parse_json(self)
                prize_id = int(body.get("prize_id"))
                name = str(body.get("name", "")).strip()
                quantity = int(body.get("quantity", 0))
                if not name:
                    return self.end_json({"ok": False, "error": "请输入奖品名称"}, HTTPStatus.BAD_REQUEST)
                with DB_LOCK, connect() as conn:
                    prize = conn.execute("SELECT * FROM prizes WHERE id=?", (prize_id,)).fetchone()
                    if not prize:
                        return self.end_json({"ok": False, "error": "找不到该奖品"}, HTTPStatus.NOT_FOUND)
                    if quantity < prize["used_count"]:
                        return self.end_json({"ok": False, "error": f"数量不能小于已发出的 {prize['used_count']} 份"}, HTTPStatus.BAD_REQUEST)
                    conn.execute("UPDATE prizes SET name=?, quantity=? WHERE id=?", (name, quantity, prize_id))
                    conn.commit()
                return self.end_json({"ok": True})
            if parsed.path == "/api/prize/reset-used":
                # 测试用：把奖品的已发出数量归零，并把本奖品对应的已中奖嘉宾恢复为可抽奖。
                # 中奖记录不删除，方便测试后仍能追溯历史操作。
                body = parse_json(self)
                try:
                    prize_id = int(body.get("prize_id"))
                except (TypeError, ValueError):
                    return self.end_json({"ok": False, "error": "奖品参数无效"}, HTTPStatus.BAD_REQUEST)
                with DB_LOCK, connect() as conn:
                    prize = conn.execute("SELECT * FROM prizes WHERE id=?", (prize_id,)).fetchone()
                    if not prize:
                        return self.end_json({"ok": False, "error": "找不到该奖品"}, HTTPStatus.NOT_FOUND)
                    if conn.execute("SELECT 1 FROM sessions WHERE pending_prize_id=?", (prize_id,)).fetchone():
                        return self.end_json({"ok": False, "error": "该奖品正在进行中的抽奖中，请先结束本轮再重置"}, HTTPStatus.BAD_REQUEST)
                    stamp = now()
                    restored = conn.execute(
                        """UPDATE guests SET status='eligible', updated_at=?
                           WHERE session_id=? AND status='confirmed_winner'
                             AND guest_id IN (
                               SELECT guest_id FROM records
                                WHERE session_id=? AND action='confirmed' AND prize_name=?
                             )""",
                        (stamp, prize["session_id"], prize["session_id"], prize["name"]),
                    ).rowcount
                    conn.execute("UPDATE prizes SET used_count=0 WHERE id=?", (prize_id,))
                    conn.commit()
                return self.end_json({"ok": True, "restored": restored})
            if parsed.path == "/api/records/clear":
                body = parse_json(self)
                session_id = int(body.get("session_id", 1))
                with DB_LOCK, connect() as conn:
                    deleted = conn.execute("DELETE FROM records WHERE session_id=?", (session_id,)).rowcount
                    conn.commit()
                return self.end_json({"ok": True, "deleted": deleted})
            if parsed.path == "/api/draw":
                body = parse_json(self)
                session_id = int(body.get("session_id", 1))
                try:
                    winner_count = int(body.get("winner_count", 1))
                except (TypeError, ValueError):
                    winner_count = 0
                if winner_count < 1:
                    return self.end_json({"ok": False, "error": "单次中奖人数至少为 1 人"}, HTTPStatus.BAD_REQUEST)
                with DB_LOCK, connect() as conn:
                    existing_pending = conn.execute(
                        "SELECT 1 FROM guests WHERE session_id=? AND status='pending' LIMIT 1", (session_id,)
                    ).fetchone()
                    if existing_pending:
                        return self.end_json({"ok": False, "error": "当前有一轮待确认的抽奖，请先确认、重抽或取消"}, HTTPStatus.BAD_REQUEST)
                    candidates = conn.execute("""SELECT * FROM guests WHERE session_id=? AND checked_in=1 AND status='eligible'""", (session_id,)).fetchall()
                    if not candidates:
                        return self.end_json({"ok": False, "error": "当前没有可抽奖的已签到嘉宾"}, HTTPStatus.BAD_REQUEST)
                    prize_id = body.get("prize_id")
                    prize = conn.execute("SELECT * FROM prizes WHERE id=? AND session_id=?", (prize_id, session_id)).fetchone() if prize_id else conn.execute("SELECT * FROM prizes WHERE session_id=? AND used_count < quantity ORDER BY id LIMIT 1", (session_id,)).fetchone()
                    if not prize or prize["used_count"] >= prize["quantity"]:
                        return self.end_json({"ok": False, "error": "请选择仍有库存的奖品"}, HTTPStatus.BAD_REQUEST)
                    remaining = prize["quantity"] - prize["used_count"]
                    allowed = min(len(candidates), remaining)
                    if winner_count > allowed:
                        return self.end_json(
                            {"ok": False, "error": f"本次最多可抽 {allowed} 人（受已签到嘉宾和奖品库存限制）"},
                            HTTPStatus.BAD_REQUEST,
                        )
                    winners = random.SystemRandom().sample(candidates, winner_count)
                    stamp = now()
                    conn.executemany(
                        "UPDATE guests SET status='pending', updated_at=? WHERE guest_id=?",
                        [(stamp, winner["guest_id"]) for winner in winners],
                    )
                    conn.execute(
                        "UPDATE sessions SET pending_prize_id=?, draw_state='rolling' WHERE id=?",
                        (prize["id"], session_id),
                    )
                    conn.commit()
                return self.end_json({"ok": True, "winner": guest_payload(winners[0]), "winners": [guest_payload(winner) for winner in winners], "prize": dict(prize)})
            if parsed.path == "/api/draw/pause":
                # 停止大屏滚动并揭晓当前已选中的嘉宾；中奖记录仍要由后台另行确认。
                body = parse_json(self)
                session_id = int(body.get("session_id", 1))
                with DB_LOCK, connect() as conn:
                    pending = conn.execute(
                        "SELECT 1 FROM guests WHERE session_id=? AND status='pending'", (session_id,)
                    ).fetchone()
                    if not pending:
                        return self.end_json({"ok": False, "error": "当前没有正在进行的抽奖"}, HTTPStatus.BAD_REQUEST)
                    conn.execute("UPDATE sessions SET draw_state='paused' WHERE id=?", (session_id,))
                    conn.commit()
                return self.end_json({"ok": True})
            if parsed.path == "/api/draw/cancel":
                # 取消本轮：把 pending 的嘉宾原样放回可抽奖池（不排除、不写中奖记录），并解绑本轮奖品
                body = parse_json(self)
                session_id = int(body.get("session_id", 1))
                with DB_LOCK, connect() as conn:
                    restored = conn.execute(
                        "UPDATE guests SET status='eligible', updated_at=? WHERE session_id=? AND status='pending'",
                        (now(), session_id),
                    ).rowcount
                    conn.execute("UPDATE sessions SET pending_prize_id=NULL, draw_state='idle' WHERE id=?", (session_id,))
                    conn.commit()
                return self.end_json({"ok": True, "restored": restored})
            if parsed.path in ("/api/confirm", "/api/redraw"):
                body = parse_json(self)
                guest_id = str(body.get("guest_id") or "")
                session_id = int(body.get("session_id", 1))
                action = "confirmed" if parsed.path.endswith("confirm") else "redraw_excluded"
                note = str(body.get("note", ""))
                with DB_LOCK, connect() as conn:
                    if guest_id:
                        rows = [find_guest(conn, guest_id)]
                        rows = [row for row in rows if row and row["status"] == "pending"]
                    else:
                        rows = conn.execute(
                            "SELECT * FROM guests WHERE session_id=? AND status='pending' ORDER BY name, guest_id", (session_id,)
                        ).fetchall()
                    if not rows:
                        return self.end_json({"ok": False, "error": "当前没有等待确认的嘉宾"}, HTTPStatus.BAD_REQUEST)
                    new_status = "confirmed_winner" if action == "confirmed" else "redraw_excluded"
                    active_session_id = rows[0]["session_id"]
                    session_row = conn.execute("SELECT * FROM sessions WHERE id=?", (active_session_id,)).fetchone()
                    prize_id = session_row["pending_prize_id"] if session_row else None
                    prize = conn.execute("SELECT * FROM prizes WHERE id=?", (prize_id,)).fetchone() if prize_id else None
                    prize_name = prize["name"] if prize else ""
                    stamp = now()
                    conn.executemany(
                        "UPDATE guests SET status=?, updated_at=? WHERE guest_id=?",
                        [(new_status, stamp, row["guest_id"]) for row in rows],
                    )
                    conn.executemany(
                        "INSERT INTO records(guest_id,session_id,action,note,prize_name,created_at) VALUES (?,?,?,?,?,?)",
                        [(row["guest_id"], active_session_id, action, note, prize_name, stamp) for row in rows],
                    )
                    if action == "confirmed" and prize:
                        conn.execute("UPDATE prizes SET used_count=used_count+? WHERE id=?", (len(rows), prize_id))
                    pending_left = conn.execute(
                        "SELECT 1 FROM guests WHERE session_id=? AND status='pending' LIMIT 1", (active_session_id,)
                    ).fetchone()
                    if not pending_left:
                        conn.execute("UPDATE sessions SET pending_prize_id=NULL, draw_state='idle' WHERE id=?", (active_session_id,))
                    conn.commit()
                return self.end_json({"ok": True, "count": len(rows)})
            if parsed.path == "/api/reset":
                with DB_LOCK, connect() as conn:
                    conn.execute("DELETE FROM records")
                    conn.execute("DELETE FROM prizes")
                    conn.execute("DELETE FROM guests")
                    conn.execute("UPDATE sessions SET pending_prize_id=NULL, draw_state='idle'")
                    stamp = now()
                    conn.executemany("""INSERT INTO guests(guest_id,name,company,email,session_id,created_at,updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""", [(*g, stamp, stamp) for g in SEED_GUESTS])
                    conn.executemany("INSERT INTO prizes(session_id,name,quantity,created_at) VALUES (?,?,?,?)", [(1, "Grand Prize", 1, stamp), (1, "Lucky Prize", 2, stamp), (2, "Grand Prize", 1, stamp), (2, "Lucky Prize", 2, stamp)])
                    conn.commit()
                return self.end_json({"ok": True})
            return self.end_json({"ok": False, "error": "Not found"}, HTTPStatus.NOT_FOUND)
        except sqlite3.IntegrityError as exc:
            return self.end_json({"ok": False, "error": "Guest ID 已存在，请换一个编号"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return self.end_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, fmt, *args):
        if not str(args[1]).startswith("304"):
            super().log_message(fmt, *args)


def _wait_process_exit_windows(pid: int) -> bool:
    """阻塞直到 pid 退出。等到返回 True；拿不到句柄（进程已不存在、权限不足）返回 False。"""
    try:
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        INFINITE = 0xFFFFFFFF
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # 必须显式声明签名：64 位下 HANDLE 是 64 位，按默认 int 解析会被截断。
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD

        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        # 进程活着就一直阻塞在这里；它一结束就返回。
        kernel32.WaitForSingleObject(handle, INFINITE)
        return True
    except Exception:
        return False


def _process_alive(pid: int) -> bool:
    """POSIX 下的存活探测：signal 0 不做任何事，只做权限/存在性检查。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 进程存在但不是我们的（理论上不会发生），当作活着，别误杀自己。
        return True
    except Exception:
        return False


def watch_parent_process() -> None:
    """父进程一旦消失就自行退出。

    Electron 主进程被强杀（任务管理器「结束任务」、Force Quit、崩溃、被安全软件
    干掉）时，它的退出钩子根本不会执行，清理逻辑没机会跑。这时 PyInstaller 的
    内层「真身」就会活下来变成孤儿 —— 继续占着端口、继续连着同一个 SQLite。

    监视对象优先取环境变量 LUCKY_DRAW_PARENT_PID（就是 Electron 主进程自己的
    PID，由 main.cjs 传进来）。**不能只看 os.getppid()**：

      · Windows 上父进程死后 getppid() 不会变化（PID 只是记在进程信息里，不像
        POSIX 会被 init 收养），必须拿句柄去等它 signaled；
      · macOS / Linux 上更麻烦 —— 如果 Electron 主进程被 kill -9，外层
        bootloader 并不会跟着死（没有 Windows 的 Job 对象机制），它会继续等着
        内层，于是 getppid() 一直不变，孤儿永远不被发现。只有直接盯主进程才有效。

    所以两个平台都盯 LUCKY_DRAW_PARENT_PID；没这个变量时退回直接父进程
    （bootloader），聊胜于无。
    """
    direct_parent = os.getppid()
    try:
        watched = int(os.environ.get("LUCKY_DRAW_PARENT_PID") or 0)
    except ValueError:
        watched = 0
    if watched <= 0:
        watched = direct_parent
    if watched <= 0:
        return

    if os.name == "nt":
        if _wait_process_exit_windows(watched):
            os._exit(0)
        # 句柄拿不到（权限/时机）时退回直接父进程再试一次。
        if watched != direct_parent and _wait_process_exit_windows(direct_parent):
            os._exit(0)
        return

    # POSIX：轮询。两个信号任一成立就退 ——
    #   · 被监视的 Electron 主进程消失；
    #   · 直接父进程（bootloader）换人，说明它已经先走了，我们留着也没意义。
    while True:
        if not _process_alive(watched) or os.getppid() != direct_parent:
            os._exit(0)
        time.sleep(1)


if __name__ == "__main__":
    DB_PATH = EVENTS.database(EVENTS.active())
    init_db()
    # 守护线程：父进程没了就一起走，避免留下占着端口和 SQLite 的孤儿进程。
    threading.Thread(target=watch_parent_process, daemon=True).start()
    host = os.environ.get("LUCKY_DRAW_HOST", "127.0.0.1")
    port = int(os.environ.get("LUCKY_DRAW_PORT", "4173"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Lucky Draw local server: http://{host}:{port}/")
    print(f"SQLite database: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
