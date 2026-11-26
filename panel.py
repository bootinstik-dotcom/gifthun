"""Admin panel inside the bot (buttons + a few commands). Only ADMIN_ID can use it."""
import math
import re

from telethon import Button, errors, events, functions
from telethon.tl.types import MessageEntityCustomEmoji

import config
import market
from sender import u16

CODE_HELP = (
    "Telegram sent a login code to that account.\n"
    "Type it with dashes between the digits, like 1-2-3-4-5, so Telegram does not cancel it."
)

SETTINGS = {
    "interval": ("Check interval (seconds per collection)", float, 1, 600),
    "min_delay": ("Minimum pause between calls of one account (seconds)", float, 0.1, 10),
    "min_discount": ("Minimum discount % (general channel)", float, 0, 100),
    "max_price": ("Maximum price in stars (general channel, 0 = no limit)", float, 0, 10**9),
    "general_channel": ("General channel (@username or numeric id, '-' to disable)", str, None, None),
}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_collection(cols, name):
    n = norm(name)
    if not n:
        return None
    exact = [c for c in cols if norm(c["title"]) == n]
    if len(exact) == 1:
        return exact[0]
    part = [c for c in cols if n in norm(c["title"])]
    return part[0] if len(part) == 1 else None


def parse_emoji_lines(text, entities):
    """[(line, [custom emoji ids in that line])] — offsets are UTF-16 based."""
    ents = [e for e in (entities or []) if isinstance(e, MessageEntityCustomEmoji)]
    out, pos = [], 0
    for line in text.split("\n"):
        n = u16(line)
        out.append((line, [e.document_id for e in ents if pos <= e.offset < pos + n]))
        pos += n + 1
    return out


def clean_name(line):
    part = re.split(r"[:=|]", line)[0]
    return re.sub(r"[^\w\s'\-.]", "", part).strip()


def menu_buttons():
    return [
        [Button.inline("👥 Accounts", b"acc"), Button.inline("⚙️ Settings", b"set")],
        [Button.inline("😀 Emojis", b"emo"), Button.inline("📡 Channels", b"chan")],
        [Button.inline("📊 Status", b"status"), Button.inline("✅ Check", b"check")],
    ]


BACK = [Button.inline("« Back", b"menu")]


class Panel:
    def __init__(self, bot, db, mgr, sender):
        self.bot, self.db, self.mgr, self.sender = bot, db, mgr, sender
        self.state = None

    def register(self):
        admin = config.ADMIN_ID
        self.bot.add_event_handler(
            self.on_message,
            events.NewMessage(incoming=True, func=lambda e: e.is_private and e.sender_id == admin),
        )
        self.bot.add_event_handler(
            self.on_callback, events.CallbackQuery(func=lambda e: e.sender_id == admin)
        )

    # ------------------------------------------------------------ helpers
    async def show(self, event, text, buttons=None):
        try:
            await event.edit(text, buttons=buttons)
        except errors.MessageNotModifiedError:
            pass
        except Exception:
            await event.respond(text, buttons=buttons)

    async def main_text(self):
        return "🎁 Gift Floor Bot — admin panel"

    # ------------------------------------------------------------ messages
    async def on_message(self, event):
        text = (event.raw_text or "").strip()
        low = text.lower()

        if low in ("/start", "/menu"):
            self.state = None
            await event.respond(await self.main_text(), buttons=menu_buttons())
            return
        if low.startswith("/route"):
            return await self.cmd_route(event, text)
        if low.startswith("/unroute"):
            return await self.cmd_unroute(event, text)

        st = self.state
        if not st:
            await event.respond("Use the menu:", buttons=menu_buttons())
            return
        mode = st["mode"]

        if mode == "phone":
            phone = "+" + re.sub(r"\D", "", text)
            try:
                await self.mgr.login_start(phone, st["role"])
            except errors.PhoneNumberInvalidError:
                await event.respond("That phone number is not valid. Send it like +989121234567")
                return
            except errors.FloodWaitError as e:
                self.state = None
                await event.respond(f"Telegram says wait {e.seconds}s before asking for another code.", buttons=BACK)
                return
            except Exception as e:
                self.state = None
                await event.respond(f"Login failed: {type(e).__name__}", buttons=BACK)
                return
            self.state = {"mode": "code"}
            await event.respond(CODE_HELP, buttons=[Button.inline("Cancel", b"cancel")])

        elif mode == "code":
            await self.safe_delete(event)
            code = re.sub(r"\D", "", text)
            try:
                res = await self.mgr.login_code(code)
            except errors.PhoneCodeInvalidError:
                await event.respond("Wrong code, try again.")
                return
            except errors.PhoneCodeExpiredError:
                await self.mgr.login_cancel()
                self.state = None
                await event.respond("The code expired. Start again from Accounts → Add.", buttons=BACK)
                return
            except Exception as e:
                await self.mgr.login_cancel()
                self.state = None
                await event.respond(f"Login failed: {type(e).__name__}", buttons=BACK)
                return
            if res == "password":
                self.state = {"mode": "password"}
                await event.respond("This account has two-step verification. Send its password (the message will be deleted).")
            else:
                self.state = None
                await event.respond(f"✅ Account added (#{res}). It is active now.", buttons=[[Button.inline("👥 Accounts", b"acc")]])

        elif mode == "password":
            await self.safe_delete(event)
            try:
                res = await self.mgr.login_password(text)
            except errors.PasswordHashInvalidError:
                await event.respond("Wrong password, try again.")
                return
            except Exception as e:
                await self.mgr.login_cancel()
                self.state = None
                await event.respond(f"Login failed: {type(e).__name__}", buttons=BACK)
                return
            self.state = None
            await event.respond(f"✅ Account added (#{res}). It is active now.", buttons=[[Button.inline("👥 Accounts", b"acc")]])

        elif mode == "set":
            await self.save_setting(event, st["key"], text)

        elif mode == "emoji":
            await self.save_emojis(event)

    async def safe_delete(self, event):
        try:
            await event.delete()
        except Exception:
            pass

    async def save_setting(self, event, key, text):
        label, typ, lo, hi = SETTINGS[key]
        if typ is str:
            value = "" if text == "-" else text.strip()
            if value and not (value.startswith("@") or re.fullmatch(r"-?\d+", value)):
                await event.respond("Send an @username, a numeric id, or '-' to disable.")
                return
        else:
            try:
                value = typ(text.replace(",", "."))
            except ValueError:
                await event.respond("Send a number.")
                return
            if not (lo <= value <= hi):
                await event.respond(f"Value must be between {lo} and {hi}.")
                return
        await self.db.set(key, value)
        self.state = None
        await event.respond(f"✅ Saved: {label} = {value or '—'}", buttons=[[Button.inline("⚙️ Settings", b"set")]])

    async def save_emojis(self, event):
        m = event.message
        cols = await self.db.all_collections()
        if not cols:
            await event.respond("The collection list is still empty. Wait a minute (an account must be logged in) and try again.")
            return
        done, problems = [], []
        for line, ids in parse_emoji_lines(m.message or "", m.entities):
            if not line.strip():
                continue
            if not ids:
                problems.append(f"no premium emoji: {line.strip()[:30]}")
                continue
            col = find_collection(cols, clean_name(line))
            if not col:
                problems.append(f"collection not found: {clean_name(line) or line.strip()[:30]}")
                continue
            await self.db.set_emoji(col["id"], ids[0])
            done.append(col["title"])
        msg = ""
        if done:
            msg += "✅ Saved: " + ", ".join(done) + "\n"
        if problems:
            msg += "⚠️ " + "\n⚠️ ".join(problems)
        await event.respond(msg or "Nothing found. Format: Name: <premium emoji>",
                            buttons=[[Button.inline("Done", b"cancel")]])

    # ------------------------------------------------------------ commands
    async def cmd_route(self, event, text):
        body = text[len("/route"):].strip()
        if "=" not in body:
            await event.respond("Usage:\n/route Plush Pepe = @channel\n/route Plush Pepe = @channel 5000\n(5000 = max price in stars, optional)")
            return
        name, rest = body.split("=", 1)
        parts = rest.split()
        if not parts:
            await event.respond("Channel is missing.")
            return
        channel = parts[0]
        try:
            max_price = float(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            await event.respond("Max price must be a number.")
            return
        col = find_collection(await self.db.all_collections(), name)
        if not col:
            await event.respond(f"Collection not found: {name.strip()}")
            return
        await self.db.set_route(col["id"], channel, max_price)
        await event.respond(f"✅ {col['title']} → {channel}" + (f" (max {max_price:,.0f} ⭐)" if max_price else ""))

    async def cmd_unroute(self, event, text):
        name = text[len("/unroute"):].strip()
        col = find_collection(await self.db.all_collections(), name)
        if not col:
            await event.respond(f"Collection not found: {name}")
            return
        await self.db.set_route(col["id"], None, 0)
        await event.respond(f"✅ Removed channel of {col['title']}")

    # ------------------------------------------------------------ buttons
    async def on_callback(self, event):
        data = event.data.decode()
        await event.answer()

        if data == "menu":
            self.state = None
            await self.show(event, await self.main_text(), menu_buttons())
        elif data == "cancel":
            self.state = None
            await self.mgr.login_cancel()
            await self.show(event, await self.main_text(), menu_buttons())
        elif data == "acc":
            await self.view_accounts(event)
        elif data.startswith("acc:"):
            await self.view_account(event, int(data[4:]))
        elif data.startswith("add:"):
            self.state = {"mode": "phone", "role": data[4:]}
            await self.show(event, "Send the phone number in international format, e.g. +989121234567",
                            [[Button.inline("Cancel", b"cancel")]])
        elif data.startswith("act:"):
            await self.mgr.toggle(int(data[4:]))
            await self.view_accounts(event)
        elif data.startswith("rm:"):
            aid = data[3:]
            await self.show(event, f"Remove account #{aid}? Its session will be logged out.",
                            [[Button.inline("Yes, remove", f"rmy:{aid}".encode()), Button.inline("No", b"acc")]])
        elif data.startswith("rmy:"):
            await self.mgr.remove(int(data[4:]))
            await self.view_accounts(event)
        elif data == "set":
            await self.view_settings(event)
        elif data == "tog:enabled":
            on = await self.db.get("enabled", "1") == "1"
            await self.db.set("enabled", "0" if on else "1")
            await self.view_settings(event)
        elif data.startswith("sv:"):
            key = data[3:]
            self.state = {"mode": "set", "key": key}
            await self.show(event, f"{SETTINGS[key][0]}\n\nSend the new value.", [[Button.inline("Cancel", b"cancel")]])
        elif data == "emo":
            await self.view_emoji(event)
        elif data == "chan":
            await self.view_channels(event)
        elif data == "status":
            await self.view_status(event)
        elif data == "check":
            await self.show(event, "Checking…")
            await self.show(event, await self.run_check(), [BACK])

    # ------------------------------------------------------------ views
    async def view_accounts(self, event):
        accs = await self.db.accounts()
        rows = []
        for a in accs:
            icon = "🟢" if a["status"] == "ready" and a["enabled"] else "⚪️" if not a["enabled"] else "🔴"
            rows.append([Button.inline(f"{icon} #{a['id']} {a['phone']} · {a['role']}", f"acc:{a['id']}".encode())])
        rows.append([Button.inline("➕ Reader", b"add:reader"), Button.inline("➕ Sender (Premium)", b"add:sender")])
        rows.append(BACK)
        text = ("👥 Accounts\n\n"
                "Readers scan the market (no Premium needed). The sender posts to channels with your "
                "premium emoji (use your Premium account).")
        if not accs:
            text += "\n\nNo accounts yet."
        await self.show(event, text, rows)

    async def view_account(self, event, aid):
        a = await self.db.account(aid)
        if not a:
            return await self.view_accounts(event)
        text = f"#{a['id']} {a['phone']}\nRole: {a['role']}\nStatus: {a['status']}\nEnabled: {'yes' if a['enabled'] else 'no'}"
        await self.show(event, text, [
            [Button.inline("⏸ Turn off" if a["enabled"] else "▶️ Turn on", f"act:{aid}".encode()),
             Button.inline("🗑 Remove", f"rm:{aid}".encode())],
            [Button.inline("« Back", b"acc")],
        ])

    async def view_settings(self, event):
        s = await self.db.all_settings()
        on = s["enabled"] == "1"
        text = (
            "⚙️ Settings\n\n"
            f"Scanner: {'ON' if on else 'PAUSED'}\n"
            f"Interval: {s['interval']} s per collection\n"
            f"Min call delay: {s['min_delay']} s\n"
            f"General channel: {s['general_channel'] or '—'}\n"
            f"Min discount: {s['min_discount']}%\n"
            f"Max price: {s['max_price']} ⭐ (0 = no limit)"
        )
        await self.show(event, text, [
            [Button.inline("⏱ Interval", b"sv:interval"), Button.inline("🐢 Min delay", b"sv:min_delay")],
            [Button.inline("📣 General channel", b"sv:general_channel")],
            [Button.inline("📉 Min discount", b"sv:min_discount"), Button.inline("💰 Max price", b"sv:max_price")],
            [Button.inline("⏸ Pause" if on else "▶️ Resume", b"tog:enabled")],
            BACK,
        ])

    async def view_emoji(self, event):
        cols = await self.db.all_collections()
        missing = [c["title"] for c in cols if c["resale"] > 0 and not c["emoji_id"]]
        self.state = {"mode": "emoji"}
        text = (
            "😀 Collection emojis\n\n"
            "Send a message with one line per collection, a premium emoji after the name:\n\n"
            "Plush Pepe: <emoji>\nDurov's Cap: <emoji>\n\n"
            "I read the emoji codes from your message. Send as many messages as you like, then press Done."
        )
        if missing:
            text += f"\n\nWithout emoji yet ({len(missing)}): " + ", ".join(missing[:40])
        elif cols:
            text += "\n\nAll active collections have an emoji."
        await self.show(event, text, [[Button.inline("Done", b"cancel")]])

    async def view_channels(self, event):
        cols = [c for c in await self.db.all_collections() if c["channel"]]
        s = await self.db.all_settings()
        text = f"📡 Channels\n\nGeneral: {s['general_channel'] or '—'}\n\n"
        text += "\n".join(
            f"{c['title']} → {c['channel']}" + (f" (max {c['max_price']:,.0f} ⭐)" if c["max_price"] else "")
            for c in cols
        ) or "No collection channels yet."
        text += ("\n\nCommands:\n/route Plush Pepe = @channel [max price]\n/unroute Plush Pepe\n\n"
                 "The sender account must be an admin of every channel.")
        await self.show(event, text, [BACK])

    async def view_status(self, event):
        s = await self.db.all_settings()
        cols = await self.db.active_collections()
        readers = self.mgr.ready_readers()
        accs = await self.db.accounts()
        text = [
            "📊 Status",
            f"Scanner: {'ON' if s['enabled'] == '1' else 'PAUSED'}",
            f"Readers ready: {len(readers)} / {sum(1 for a in accs if a['role'] == 'reader')}",
            f"Sender: {'ready' if self.mgr.sender_client() else 'NOT connected'}",
            f"Collections with listings: {len(cols)}",
            f"Polls: {self.mgr.stats['polls']} · Alerts: {self.mgr.stats['alerts']} · Posted: {self.sender.sent}",
            f"Errors: {self.mgr.stats['errors']} · Queue: {self.mgr.queue.qsize()}",
        ]
        if cols and readers:
            interval, min_delay = float(s["interval"]), float(s["min_delay"])
            shard = math.ceil(len(cols) / len(readers))
            cycle = shard * max(min_delay, interval / shard)
            text.append(f"Estimated time to scan every collection once: {cycle:.1f} s")
            need = math.ceil(len(cols) * min_delay / interval)
            if cycle > interval * 1.2:
                text.append(f"⚠️ Slower than your {interval:g}s target. About {need} reader accounts are needed.")
        await self.show(event, "\n".join(text), [BACK])

    async def run_check(self):
        import telethon
        from telethon.tl.alltlobjects import LAYER

        lines = [f"Telethon {telethon.__version__} (layer {LAYER})"]
        for name in ("GetStarGiftsRequest", "GetResaleStarGiftsRequest"):
            ok = hasattr(functions.payments, name)
            lines.append(("✅ " if ok else "❌ ") + name)
        client = self.mgr.any_client()
        if not client:
            lines.append("No logged-in account to test with.")
        else:
            try:
                cols = await market.fetch_collections(client)
                lines.append(f"✅ Collections with listings: {len(cols)}")
                if cols:
                    ls = await market.fetch_listings(client, cols[0]["id"], 3)
                    lines.append(f"✅ Listings read for {cols[0]['title']}: {len(ls)}")
                    if ls:
                        l = ls[0]
                        lines.append(f"Sample: #{l['num']} {l['amount']} {l['cur']} · {l['model'][0]} / {l['backdrop'][0]} / {l['symbol'][0]}")
            except Exception as e:
                lines.append(f"❌ {type(e).__name__}: {e}")
        return "\n".join(lines)
