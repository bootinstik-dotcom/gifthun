import time

import aiosqlite

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT UNIQUE,
    session TEXT,
    role TEXT,
    enabled INTEGER DEFAULT 1,
    status TEXT DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS collections(
    id INTEGER PRIMARY KEY,
    title TEXT,
    resale INTEGER DEFAULT 0,
    floor_json TEXT DEFAULT '',
    emoji_id INTEGER,
    channel TEXT,
    max_price REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, ts INTEGER);
"""


class DB:
    def __init__(self, path=None):
        self.path = path or config.DB_PATH
        self.conn = None

    async def init(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        upgrading = (await self.get("interval")) is not None and (await self.get("schema_v")) is None
        for k, v in config.DEFAULTS.items():
            await self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        if upgrading:  # v1 -> v2: new default of 1.5% minimum discount
            await self.conn.execute("UPDATE settings SET value='1.5' WHERE key='min_discount'")
        await self.conn.execute("DELETE FROM seen WHERE ts < ?", (int(time.time()) - 14 * 86400,))
        await self.conn.commit()

    # ---- settings ----
    async def get(self, key, default=None):
        cur = await self.conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set(self, key, value):
        await self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await self.conn.commit()

    async def all_settings(self):
        cur = await self.conn.execute("SELECT key,value FROM settings")
        return {r["key"]: r["value"] for r in await cur.fetchall()}

    # ---- accounts ----
    async def accounts(self):
        cur = await self.conn.execute("SELECT * FROM accounts ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def account(self, aid):
        cur = await self.conn.execute("SELECT * FROM accounts WHERE id=?", (aid,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def add_account(self, phone, session, role):
        await self.conn.execute(
            "INSERT INTO accounts(phone,session,role,enabled,status) VALUES(?,?,?,1,'ready') "
            "ON CONFLICT(phone) DO UPDATE SET session=excluded.session, role=excluded.role, "
            "enabled=1, status='ready'",
            (phone, session, role),
        )
        await self.conn.commit()
        cur = await self.conn.execute("SELECT id FROM accounts WHERE phone=?", (phone,))
        return (await cur.fetchone())["id"]

    async def set_account_status(self, aid, status):
        await self.conn.execute("UPDATE accounts SET status=? WHERE id=?", (status, aid))
        await self.conn.commit()

    async def set_account_enabled(self, aid, enabled):
        await self.conn.execute("UPDATE accounts SET enabled=? WHERE id=?", (1 if enabled else 0, aid))
        await self.conn.commit()

    async def delete_account(self, aid):
        await self.conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
        await self.conn.commit()

    # ---- collections ----
    async def upsert_collections(self, items):
        await self.conn.execute("UPDATE collections SET resale=0")
        await self.conn.executemany(
            "INSERT INTO collections(id,title,resale) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, resale=excluded.resale",
            [(i["id"], i["title"], i["resale"]) for i in items],
        )
        await self.conn.commit()

    async def active_collections(self):
        cur = await self.conn.execute("SELECT * FROM collections WHERE resale>0 ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def all_collections(self):
        cur = await self.conn.execute("SELECT * FROM collections ORDER BY title")
        return [dict(r) for r in await cur.fetchall()]

    async def collection(self, cid):
        cur = await self.conn.execute("SELECT * FROM collections WHERE id=?", (cid,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def set_emoji(self, cid, emoji_id):
        await self.conn.execute("UPDATE collections SET emoji_id=? WHERE id=?", (emoji_id, cid))
        await self.conn.commit()

    async def set_route(self, cid, channel, max_price=0):
        await self.conn.execute(
            "UPDATE collections SET channel=?, max_price=? WHERE id=?", (channel, max_price, cid)
        )
        await self.conn.commit()

    async def set_floor(self, cid, floor_json):
        await self.conn.execute("UPDATE collections SET floor_json=? WHERE id=?", (floor_json, cid))
        await self.conn.commit()

    # ---- seen listings ----
    async def seen_keys(self):
        cur = await self.conn.execute("SELECT key FROM seen")
        return [r["key"] for r in await cur.fetchall()]

    async def seen_add_many(self, keys):
        if not keys:
            return
        now = int(time.time())
        await self.conn.executemany(
            "INSERT OR IGNORE INTO seen(key,ts) VALUES(?,?)", [(k, now) for k in keys]
        )
        await self.conn.commit()
