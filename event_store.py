"""Local event catalog. Each event owns a separate draw database and image assets."""
import base64
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


class EventStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'events.sqlite'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO events VALUES (?,?)', ('default', json.dumps({
                'name': '原有活动', 'date': '', 'tagline': '欢迎参与现场抽奖', 'accent': '#2563eb',
                'revision': 0,
            }, ensure_ascii=False)))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('active','default')")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def active(self):
        with self.connect() as db:
            return db.execute("SELECT value FROM settings WHERE key='active'").fetchone()[0]

    def get(self, event_id):
        with self.connect() as db:
            row = db.execute('SELECT config FROM events WHERE id=?', (event_id,)).fetchone()
        if not row:
            raise ValueError('找不到该活动')
        return {'id': event_id, **json.loads(row[0])}

    def list(self):
        with self.connect() as db:
            return [{'id': row['id'], 'name': json.loads(row['config'])['name']}
                    for row in db.execute('SELECT * FROM events ORDER BY rowid')]

    def database(self, event_id):
        self.get(event_id)  # Validate before resolving any filesystem path.
        if event_id == 'default':
            return self.root / 'lucky-draw.sqlite'
        folder = self.root / 'events' / event_id
        folder.mkdir(parents=True, exist_ok=True)
        return folder / 'lucky-draw.sqlite'

    def select(self, event_id):
        self.get(event_id)
        with self.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE key='active'", (event_id,))

    def save(self, body, create=False):
        event_id = uuid.uuid4().hex if create else str(body.get('id', ''))
        previous = {} if create else self.get(event_id)
        name = str(body.get('name', '')).strip()
        if not name or len(name) > 80:
            raise ValueError('活动名称请填写 1–80 个字')
        accent = str(body.get('accent', '#2563eb'))
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', accent):
            raise ValueError('主题颜色无效')
        config = {'name': name, 'date': str(body.get('date', ''))[:80],
                  'tagline': str(body.get('tagline', ''))[:120], 'accent': accent,
                  'revision': previous.get('revision', 0) + 1}
        for key in ('logo', 'background'):
            value = body.get(key, previous.get(key, ''))
            if value:
                if not isinstance(value, str) or len(value) > 5_600_000:
                    raise ValueError('单张图片不能超过 4 MB')
                match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)', value)
                if not match:
                    raise ValueError('请使用 PNG、JPG 或 WebP 图片')
                raw = base64.b64decode(match[2], validate=True)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError('单张图片不能超过 4 MB')
                valid = ((match[1] == 'png' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or
                         (match[1] == 'jpeg' and raw.startswith(b'\xff\xd8\xff')) or
                         (match[1] == 'webp' and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP'))
                if not valid:
                    raise ValueError('图片格式与内容不一致')
            config[key] = value or ''
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO events VALUES (?,?)',
                       (event_id, json.dumps(config, ensure_ascii=False)))
        return event_id

    def public(self):
        config = self.get(self.active())
        for key in ('logo', 'background'):
            config[key] = (f"/api/event-asset?id={config['id']}&kind={key}&v={config['revision']}"
                           if config.get(key) else '')
        return config
