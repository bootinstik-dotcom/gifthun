"""Builds the channel message and posts it with the Premium sender account."""
import asyncio
import logging

from telethon import errors
from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCustomEmoji,
    MessageEntityTextUrl,
)

log = logging.getLogger("sender")


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


def money(cur, amount):
    return f"{amount:,.0f} ⭐" if cur == "XTR" else f"{amount:,.2f} TON"


def attr_text(pair):
    name, permille = pair
    if not name:
        return "—"
    return f"{name} ({permille / 10:g}%)" if permille else name


def build(alert):
    l, col, pf = alert["l"], alert["col"], alert["pf"]
    m = Msg()
    m.add("🎁", emoji=col.get("emoji_id"))
    title = l.get("title") or col.get("title") or "Gift"
    m.add(f" {title} #{l['num']}", bold=True).add("\n\n")
    m.add("🧬 Model: ").add(attr_text(l["model"])).add("\n")
    m.add("🎨 Backdrop: ").add(attr_text(l["backdrop"])).add("\n")
    m.add("🔣 Symbol: ").add(attr_text(l["symbol"])).add("\n\n")
    m.add("💰 Price: ").add(money(l["cur"], l["amount"]), bold=True).add("\n")
    if pf > l["amount"]:
        pct = (pf - l["amount"]) / pf * 100
        m.add(f"📉 Previous floor: {money(l['cur'], pf)} (−{pct:.1f}%)\n")
    else:
        m.add("📉 At floor\n")
    if l.get("slug"):
        m.add("🔗 ").add("Open gift", url=f"https://t.me/nft/{l['slug']}")
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
        out = []
        general = (s.get("general_channel") or "").strip()
        if general:
            max_price = float(s.get("max_price") or 0)
            min_disc = float(s.get("min_discount") or 0)
            if (max_price <= 0 or l["cur"] != "XTR" or l["amount"] <= max_price) and discount(alert) >= min_disc:
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
            await self.notify("nosender", "⚠️ No sender account. Add your Premium account as 'sender' in Accounts.")
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
                await self.notify(f"se{ch}", f"⚠️ Could not post to {ch}: {type(e).__name__}: {e}\n"
                                              "Make sure the sender account is an admin of that channel.")
            await asyncio.sleep(0.5)

    async def run(self):
        while True:
            alert = await self.mgr.queue.get()
            try:
                await self.post(alert)
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                await self.mgr.queue.put(alert)
            except Exception:
                log.exception("post failed")
            await asyncio.sleep(1.0)
