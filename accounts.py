"""Account manager: logins, reader loops (market scanning) and floor detection."""
import asyncio
import json
import logging
import time
from collections import deque

from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession

import config
import market

log = logging.getLogger("accounts")


class FairQueue:
    """Alerts are served round-robin across collections so one busy collection cannot flood the channel."""

    def __init__(self, max_per=10):
        self.d, self.order, self.max_per = {}, deque(), max_per
        self.ev = asyncio.Event()

    def put(self, key, item):
        q = self.d.setdefault(key, deque())
        if key not in self.order:
            self.order.append(key)
        if len(q) >= self.max_per:
            q.popleft()
        q.append(item)
        self.ev.set()

    async def get(self):
        while True:
            while self.order:
                key = self.order.popleft()
                q = self.d.get(key)
                if q:
                    item = q.popleft()
                    if q:
                        self.order.append(key)
                    else:
                        self.d.pop(key, None)
                    return item
                self.d.pop(key, None)
            self.ev.clear()
            await self.ev.wait()

    def qsize(self):
        return sum(len(q) for q in self.d.values())


class Manager:
    def __init__(self, db, notify):
        self.db = db
        self.notify = notify
        self.clients = {}   # account id -> TelegramClient
        self.roles = {}     # account id -> "reader" | "sender"
        self.tasks = {}     # account id -> reader task
        self.cool = {}      # account id -> unix time until which it rests (FloodWait)
        self.flow = None    # login flow in progress
        self.queue = FairQueue()
        self.seen = set()
        self.stats = {"polls": 0, "alerts": 0, "errors": 0}

    # ------------------------------------------------------------ startup
    async def start(self):
        self.seen = set(await self.db.seen_keys())
        for acc in await self.db.accounts():
            if acc["enabled"]:
                await self.connect(acc)
        asyncio.create_task(self.collections_loop())

    async def connect(self, acc):
        aid = acc["id"]
        try:
            client = TelegramClient(StringSession(acc["session"]), config.API_ID, config.API_HASH)
            await client.connect()
            if not await client.is_authorized():
                await client.disconnect()
                await self.db.set_account_status(aid, "logged_out")
                await self.notify(f"lo{aid}", f"⚠️ Account {acc['phone']} is logged out. Remove it and add it again.")
                return False
        except Exception as e:
            log.exception("connect failed")
            await self.db.set_account_status(aid, "error")
            await self.notify(f"ce{aid}", f"⚠️ Could not connect {acc['phone']}: {type(e).__name__}")
            return False
        self._attach(aid, acc["role"], client)
        await self.db.set_account_status(aid, "ready")
        return True

    def _attach(self, aid, role, client):
        self.clients[aid] = client
        self.roles[aid] = role
        if role == "reader":
            self.tasks[aid] = asyncio.create_task(self.reader_loop(aid))

    async def stop(self, aid):
        task = self.tasks.pop(aid, None)
        if task:
            task.cancel()
        client = self.clients.pop(aid, None)
        self.roles.pop(aid, None)
        self.cool.pop(aid, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def toggle(self, aid):
        acc = await self.db.account(aid)
        if not acc:
            return None
        if acc["enabled"]:
            await self.db.set_account_enabled(aid, False)
            await self.db.set_account_status(aid, "disabled")
            await self.stop(aid)
            return False
        await self.db.set_account_enabled(aid, True)
        await self.connect(acc)
        return True

    async def remove(self, aid):
        client = self.clients.get(aid)
        if client:
            try:
                await client.log_out()  # ends this session on the account
            except Exception:
                pass
        await self.stop(aid)
        await self.db.delete_account(aid)

    # ------------------------------------------------------------ login flow
    async def login_start(self, phone, role):
        await self.login_cancel()
        client = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except Exception:
            await client.disconnect()
            raise
        self.flow = {"client": client, "phone": phone, "hash": sent.phone_code_hash, "role": role}
        return type(getattr(sent, "type", None)).__name__

    async def login_resend(self):
        f = self.flow
        res = await f["client"](
            functions.auth.ResendCodeRequest(phone_number=f["phone"], phone_code_hash=f["hash"])
        )
        f["hash"] = res.phone_code_hash
        return type(getattr(res, "type", None)).__name__

    async def add_session(self, session_str, role):
        client = TelegramClient(StringSession(session_str.strip()), config.API_ID, config.API_HASH)
        await client.connect()
        if not await client.is_authorized():
            await client.disconnect()
            raise ValueError("not_authorized")
        me = await client.get_me()
        phone = "+" + (me.phone or str(me.id))
        old = await self._id_by_phone(phone)
        if old is not None:
            await self.stop(old)
        aid = await self.db.add_account(phone, client.session.save(), role)
        self._attach(aid, role, client)
        return aid, phone

    async def login_cancel(self):
        if self.flow:
            try:
                await self.flow["client"].disconnect()
            except Exception:
                pass
            self.flow = None

    async def login_code(self, code):
        f = self.flow
        try:
            await f["client"].sign_in(f["phone"], code, phone_code_hash=f["hash"])
        except errors.SessionPasswordNeededError:
            return "password"
        return await self._finish()

    async def login_password(self, password):
        await self.flow["client"].sign_in(password=password)
        return await self._finish()

    async def _finish(self):
        f = self.flow
        client = f["client"]
        session = client.session.save()
        old = await self._id_by_phone(f["phone"])
        if old is not None:
            await self.stop(old)
        aid = await self.db.add_account(f["phone"], session, f["role"])
        self._attach(aid, f["role"], client)
        self.flow = None
        return aid

    async def _id_by_phone(self, phone):
        for a in await self.db.accounts():
            if a["phone"] == phone:
                return a["id"]
        return None

    # ------------------------------------------------------------ helpers
    def is_cooling(self, aid):
        return self.cool.get(aid, 0) > time.time()

    def ready_readers(self):
        return sorted(
            a for a, r in self.roles.items()
            if r == "reader" and a in self.clients and not self.is_cooling(a)
        )

    def sender_client(self):
        for aid in sorted(self.roles):
            if self.roles[aid] == "sender" and aid in self.clients:
                return self.clients[aid]
        return None

    def any_client(self):
        for aid in self.ready_readers():
            return self.clients[aid]
        return self.sender_client()

    # ------------------------------------------------------------ collections
    async def collections_loop(self):
        while True:
            wait = 15
            try:
                client = self.any_client()
                if client:
                    items = await market.fetch_collections(client)
                    await self.db.upsert_collections(items)
                    wait = 600 if items else 30
            except errors.FloodWaitError as e:
                wait = e.seconds + 5
            except Exception:
                log.exception("collections refresh failed")
            await asyncio.sleep(wait)

    # ------------------------------------------------------------ scanning
    async def reader_loop(self, aid):
        loop = asyncio.get_running_loop()
        client = self.clients[aid]
        staggered = False
        while True:
            try:
                if await self.db.get("enabled", "1") != "1" or self.is_cooling(aid):
                    await asyncio.sleep(2)
                    continue
                readers = self.ready_readers()
                if aid not in readers:
                    await asyncio.sleep(2)
                    continue
                idx, n = readers.index(aid), len(readers)
                cols = await self.db.active_collections()
                shard = [c for i, c in enumerate(cols) if i % n == idx]
                if not shard:
                    await asyncio.sleep(5)
                    continue
                interval = float(await self.db.get("interval", "3"))
                min_delay = float(await self.db.get("min_delay", "0.25"))
                delay = max(min_delay, interval / len(shard))
                if not staggered:  # readers start at different moments so calls are spread out
                    staggered = True
                    await asyncio.sleep(idx / n * interval)
                for col in shard:
                    t0 = loop.time()
                    await self.poll(client, col["id"])
                    await asyncio.sleep(max(0.0, delay - (loop.time() - t0)))
            except asyncio.CancelledError:
                raise
            except errors.FloodWaitError as e:
                self.cool[aid] = time.time() + e.seconds + 1
                log.warning("account %s flood wait %ss", aid, e.seconds)
                if e.seconds >= 60:
                    await self.notify(f"fw{aid}", f"⏳ Account #{aid} is resting for {e.seconds}s (FloodWait). "
                                                   "Add more reader accounts or raise the interval.")
            except (errors.AuthKeyUnregisteredError, errors.UserDeactivatedError,
                    errors.UserDeactivatedBanError, errors.SessionRevokedError) as e:
                await self.db.set_account_status(aid, "banned_or_revoked")
                await self.notify(f"bn{aid}", f"🚫 Account #{aid} was revoked or banned ({type(e).__name__}).")
                await self.stop(aid)
                return
            except Exception:
                self.stats["errors"] += 1
                log.exception("reader %s error", aid)
                await asyncio.sleep(3)

    async def poll(self, client, cid):
        col = await self.db.collection(cid)
        listings = await market.fetch_listings(client, cid, 20)
        self.stats["polls"] += 1
        if not listings:
            return
        prev = json.loads(col["floor_json"] or "{}")
        first = not prev

        mins = {}
        for l in listings:
            mins[l["cur"]] = min(mins.get(l["cur"], float("inf")), l["amount"])

        new_keys, alerts = [], []
        for l in listings:
            key = f"{l['uid']}:{l['cur']}:{l['amount']}"
            if key in self.seen:
                continue
            self.seen.add(key)
            new_keys.append(key)
            if first:
                continue  # baseline run: remember listings, do not alert
            pf = prev.get(l["cur"])
            if pf is not None and l["amount"] <= pf:
                alerts.append((l, pf))

        await self.db.seen_add_many(new_keys)
        if mins != prev:
            await self.db.set_floor(cid, json.dumps(mins))

        for l, pf in alerts:
            self.stats["alerts"] += 1
            self.queue.put(cid, {"l": l, "pf": pf, "col": col})
