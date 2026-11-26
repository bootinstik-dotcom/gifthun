"""Builds the channel message and posts it with the Premium sender account."""
import asyncio
import logging

from telethon import errors
from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCustomEmoji,
    MessageEntityTextUrl,
)

import config

log = logging.getLogger("sender")

# Fixed premium emoji for each row of the post.
E = {
    "model": 5931588842116091655,
    "backdrop": 5814690801665446789,
    "symbol": 6008258140108231117,
    "price": 5985598781712767394,
    "star": 5836981286794699269,
    "floor": 5994378914636500516,
    "disc": 5931472654660800739,
    "link": 6008124493610885197,
    "ton": 5872872125169146997,
    "foot": 5771695636411847302,
}


def u16(s):
    return len(s.encode("utf-16-le")) // 2


class Msg:
    """Text + entities builder (offsets are in UTF-16 units, as Telegram wants)."""

    def __init__(self):
        self.text = ""
        self.ents = []

    def add(self, s, bold=False, url=None, emoji=None):
        off, n = u16(self.text), u16(s)
        self.text += s
        if emoji:
            self.ents.append(MessageEntityCustomEmoji(off, n, int(emoji)))
        if bold:
            self.ents.append(MessageEntityBold(off, n))
        if url:
            self.ents.append(MessageEntityTextUrl(off, n, url))
        return self


def num(cur, amount):
    return f"{amount:,.0f}" if cur == "XTR" else f"{amount:,.2f}"


def attr_text(pair):
    name, permille = pair
    if not name:
        return "—"
    return f"{name} ({permille / 10:g}%)" if permille else name


def unit(m, cur):
    if cur == "XTR":
        m.add("⭐", emoji=E["star"])
    else:
        m.add("💎", emoji=E["ton"])


def build(alert):
    l, col, pf = alert["l"], alert["col"], alert["pf"]
    cur = l["cur"]
    m = Msg()
    m.add("🎁", emoji=col.get("emoji_id"))
    m.add(f" {l.get('title') or col.get('title') or 'Gift'} #{l['num']}", bold=True)
    if cur == "TON":
        m.add("\n").add("💎", emoji=E["ton"]).add(" Listed in TON", bold=True)
    m.add("\n\n")
    m.add("🧬", emoji=E["model"]).add(f" Model: {attr_text(l['model'])}\n")
    m.add("🎨", emoji=E["backdrop"]).add(f" Backdrop: {attr_text(l['backdrop'])}\n")
    m.add("🔣", emoji=E["symbol"]).add(f" Symbol: {attr_text(l['symbol'])}\n")
    m.add("💰", emoji=E["price"]).add(" Price: ").add(num(cur, l["amount"]), bold=True).add(" ")
    unit(m, cur)
    m.add("\n")
    m.add("📉", emoji=E["floor"]).add(f" Previous floor: {num(cur, pf)} ")
    unit(m, cur)
    m.add("\n")
    if pf > l["amount"]:
        m.add("🔻", emoji=E["disc"]).add(f" (−{(pf - l['amount']) / pf * 100:.1f}%)\n")
    if l.get("slug"):
        m.add("🔗", emoji=E["link"]).add(" ").add("Open gift", url=f"https://t.me/nft/{l['slug']}")
    m.add("\n\n").add("📣", emoji=E["foot"]).add(f" {config.FOOTER_TAG}")
    return m.text.rstrip(), m.ents


def discount(alert):
    pf, amt = alert["pf"], alert["l"]["amount"]
    return (pf - amt) / pf * 100 if pf > 0 else 0.0


class Sender:
    def __init__(self, mgr, db, notify):
        self.mgr, self.db, self.notify = mgr, db, notify
        self.cache = {}
        self.sent = 0

    async def targets(self, alert):
        s = await self.db.all_settings()
        l, col = alert["l"], alert["col"]
        if l["cur"] == "TON" and s.get("ton_enabled", "1") != "1":
            return []
        if discount(alert) < float(s.get("min_discount") or 0):
            return []
        out = []
        general = (s.get("general_channel") or "").strip()
        max_price = float(s.get("max_price") or 0)
        if general and (max_price <= 0 or l["cur"] != "XTR" or l["amount"] <= max_price):
            out.append(general)
        fresh = await self.db.collection(col["id"]) or col
        alert["col"] = fresh
        if fresh.get("channel"):
            cmax = float(fresh.get("max_price") or 0)
            if cmax <= 0 or l["cur"] != "XTR" or l["amount"] <= cmax:
                out.append(fresh["channel"])
        return out

    async def resolve(self, client, channel):
        key = (id(client), channel)
        if key in self.cache:
            return self.cache[key]
        ref = int(channel) if channel.lstrip("-").isdigit() else channel
        try:
            ent = await client.get_entity(ref)
        except ValueError:
            await client.get_dialogs(limit=None)  # fills the entity cache
            ent = await client.get_entity(ref)
        self.cache[key] = ent
        return ent

    async def post(self, alert):
        client = self.mgr.sender_client()
        if client is None:
            await self.notify("nosender", "⚠️ اکانت ارسال‌کننده وصل نیست. از بخش اکانت‌ها یک ارسال‌کننده (پریمیوم) اضافه کنید.")
            return
        text, ents = build(alert)
        for ch in await self.targets(alert):
            try:
                ent = await self.resolve(client, ch)
                await client.send_message(ent, text, formatting_entities=ents, link_preview=True)
                self.sent += 1
            except errors.FloodWaitError:
                raise
            except Exception as e:
                log.exception("send to %s failed", ch)
                await self.notify(f"se{ch}", f"⚠️ ارسال به {ch} انجام نشد: {type(e).__name__}\n"
                                              "مطمئن شوید اکانت ارسال‌کننده ادمین آن کانال است.")
            await asyncio.sleep(0.2)

    async def send_test(self, channel):
        """Posts a sample message to `channel`. Returns an error text or None."""
        client = self.mgr.sender_client()
        if client is None:
            return "اکانت ارسال‌کننده وصل نیست."
        sample = {
            "l": {"uid": 0, "slug": "PlushPepe-1", "num": 1, "title": "Test Gift", "cur": "XTR",
                  "amount": 980.0, "model": ("Model", 10), "backdrop": ("Backdrop", 20),
                  "symbol": ("Symbol", 30)},
            "col": {"emoji_id": None, "title": "Test"},
            "pf": 1000.0,
        }
        text, ents = build(sample)
        try:
            ent = await self.resolve(client, channel)
            await client.send_message(ent, text, formatting_entities=ents, link_preview=True)
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        return None

    async def run(self):
        while True:
            alert = await self.mgr.queue.get()
            try:
                await self.post(alert)
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                self.mgr.queue.put(alert["col"]["id"], alert)
            except Exception:
                log.exception("post failed")
            await asyncio.sleep(0.3)
