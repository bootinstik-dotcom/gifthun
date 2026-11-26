"""پنل مدیریت داخل ربات (دکمه‌ها + چند دستور). فقط ADMIN_ID دسترسی دارد."""
import math
import re

from telethon import Button, errors, events, functions

import config
import market

SETTINGS = {
    "interval": ("فاصله‌ی بررسی هر کالکشن (ثانیه)", float, 1, 600),
    "min_delay": ("حداقل مکث بین درخواست‌های هر اکانت (ثانیه)", float, 0.1, 10),
    "min_discount": ("حداقل درصد زیر فلور", float, 0, 100),
    "concurrency": ("تعداد درخواست هم‌زمان هر اسکنر (۱ تا ۸)", float, 1, 8),
    "cold_factor": ("کالکشن‌های کم‌تحرک چند برابر کندتر بررسی شوند (۱ = همه یکسان)", float, 1, 10),
    "max_price": ("سقف قیمت کانال کلی (استارز، 0 = بدون محدودیت)", float, 0, 10**9),
    "general_channel": ("کانال کلی (@یوزرنیم یا آیدی عددی، با - غیرفعال می‌شود)", str, None, None),
}

ROLE_FA = {"reader": "اسکنر", "sender": "ارسال‌کننده"}
STATUS_FA = {"ready": "فعال", "disabled": "خاموش", "logged_out": "لاگ‌اوت شده", "error": "خطا",
             "banned_or_revoked": "بن/لغو شده", "new": "جدید"}
SENT_FA = {
    "SentCodeTypeApp": "داخل خود تلگرام (اپ) روی دستگاه دیگر همان اکانت",
    "SentCodeTypeSms": "پیامک",
    "SentCodeTypeCall": "تماس تلفنی",
    "SentCodeTypeFlashCall": "فلش‌کال",
    "SentCodeTypeMissedCall": "میس‌کال",
    "SentCodeTypeFragmentSms": "پیامک از طریق Fragment",
    "SentCodeTypeFirebaseSms": "پیامک",
    "SentCodeTypeEmailCode": "ایمیل",
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


def first_custom_emoji(message):
    for e in message.entities or []:
        if type(e).__name__ == "MessageEntityCustomEmoji":
            return e.document_id
    return None


def coll_report(st):
    """Human-readable result of the last collection refresh."""
    if not st.get("checked"):
        return "هنوز بررسی انجام نشده."
    if st.get("error"):
        return f"❌ خطا: {st['error']}"
    s = st.get("stats") or {}
    lines = [f"✅ کالکشن‌های دارای لیست: {st['count']}",
             f"گیفت‌های دریافت‌شده از تلگرام: {s.get('raw')} · دارای شمارنده‌ی فروش مجدد: {s.get('with_attr')}"]
    if st["count"] == 0:
        lines.append("فیلدهای اولین گیفت: " + ", ".join(s.get("fields") or []) [:300])
        if s.get("raw") and not s.get("with_attr"):
            lines.append("⚠️ نسخه‌ی Telethon قدیمی است و فروش مجدد را نمی‌شناسد. دیپلوی را بدون کش دوباره بزنید.")
    return "\n".join(lines)


def menu_buttons():
    return [
        [Button.inline("👥 اکانت‌ها", b"acc"), Button.inline("⚙️ تنظیمات", b"set")],
        [Button.inline("😀 ایموجی‌ها", b"ep:0"), Button.inline("📡 کانال‌ها", b"chan")],
        [Button.inline("📊 وضعیت", b"status"), Button.inline("✅ بررسی و تست", b"check")],
    ]


BACK = [Button.inline("« بازگشت", b"menu")]
CANCEL = [Button.inline("لغو", b"cancel")]


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

    async def show(self, event, text, buttons=None):
        try:
            await event.edit(text, buttons=buttons)
        except errors.MessageNotModifiedError:
            pass
        except Exception:
            await event.respond(text, buttons=buttons)

    async def safe_delete(self, event):
        try:
            await event.delete()
        except Exception:
            pass

    # ------------------------------------------------------------ پیام‌ها
    async def on_message(self, event):
        text = (event.raw_text or "").strip()
        low = text.lower()

        if low in ("/start", "/menu"):
            self.state = None
            await self.mgr.login_cancel()
            await event.respond("🎁 ربات فلور گیفت — پنل مدیریت", buttons=menu_buttons())
            return
        if low.startswith("/route"):
            return await self.cmd_route(event, text)
        if low.startswith("/unroute"):
            return await self.cmd_unroute(event, text)

        st = self.state
        if not st:
            await event.respond("از منوی زیر استفاده کنید:", buttons=menu_buttons())
            return
        mode = st["mode"]

        if mode == "phone":
            phone = "+" + re.sub(r"\D", "", text)
            try:
                kind = await self.mgr.login_start(phone, st["role"])
            except errors.PhoneNumberInvalidError:
                await event.respond("شماره معتبر نیست. به شکل +989121234567 بفرستید.")
                return
            except errors.FloodWaitError as e:
                self.state = None
                await event.respond(f"تلگرام می‌گوید {e.seconds} ثانیه صبر کنید و دوباره تلاش کنید.", buttons=[BACK])
                return
            except Exception as e:
                self.state = None
                await event.respond(f"ورود انجام نشد: {type(e).__name__}", buttons=[BACK])
                return
            self.state = {"mode": "code", "role": st["role"]}
            await event.respond(self.code_text(kind), buttons=self.code_buttons())

        elif mode == "code":
            await self.safe_delete(event)
            code = re.sub(r"\D", "", text)
            try:
                res = await self.mgr.login_code(code)
            except errors.PhoneCodeInvalidError:
                await event.respond("کد اشتباه است، دوباره بفرستید.")
                return
            except errors.PhoneCodeExpiredError:
                await event.respond("کد منقضی شده. دکمه‌ی «ارسال مجدد کد» را بزنید.", buttons=self.code_buttons())
                return
            except Exception as e:
                await self.mgr.login_cancel()
                self.state = None
                await event.respond(f"ورود انجام نشد: {type(e).__name__}", buttons=[BACK])
                return
            if res == "password":
                self.state = {"mode": "password"}
                await event.respond("این اکانت رمز دومرحله‌ای دارد. رمز را بفرستید (پیام شما پاک می‌شود).")
            else:
                self.state = None
                await event.respond(f"✅ اکانت اضافه شد (#{res}) و فعال است.", buttons=[[Button.inline("👥 اکانت‌ها", b"acc")]])

        elif mode == "password":
            await self.safe_delete(event)
            try:
                res = await self.mgr.login_password(text)
            except errors.PasswordHashInvalidError:
                await event.respond("رمز اشتباه است، دوباره بفرستید.")
                return
            except Exception as e:
                await self.mgr.login_cancel()
                self.state = None
                await event.respond(f"ورود انجام نشد: {type(e).__name__}", buttons=[BACK])
                return
            self.state = None
            await event.respond(f"✅ اکانت اضافه شد (#{res}) و فعال است.", buttons=[[Button.inline("👥 اکانت‌ها", b"acc")]])

        elif mode == "session":
            await self.safe_delete(event)  # رشته‌ی سشن محرمانه است
            try:
                aid, phone = await self.mgr.add_session(text, st["role"])
            except ValueError:
                await event.respond("این Session String معتبر نیست یا اکانت از آن خارج شده.")
                return
            except Exception as e:
                await event.respond(f"افزودن انجام نشد: {type(e).__name__}")
                return
            self.state = None
            await event.respond(f"✅ اکانت {phone} اضافه شد (#{aid}).", buttons=[[Button.inline("👥 اکانت‌ها", b"acc")]])

        elif mode == "set":
            await self.save_setting(event, st["key"], text)

        elif mode == "emoji":
            emoji_id = first_custom_emoji(event.message)
            if not emoji_id:
                await event.respond("در این پیام ایموجی پریمیوم نبود. یک ایموجی پریمیوم بفرستید.",
                                    buttons=[[Button.inline("« بازگشت به لیست", f"ep:{st['page']}".encode())]])
                return
            await self.db.set_emoji(st["cid"], emoji_id)
            self.state = None
            col = await self.db.collection(st["cid"])
            t, rows = await self.emoji_page(st["page"])
            await event.respond(f"✅ ایموجی «{col['title']}» ذخیره شد.\n\n{t}", buttons=rows)

    def code_text(self, kind):
        how = SENT_FA.get(kind, "روش نامشخص")
        return (f"کد ورود ارسال شد. روش ارسال: {how}\n\n"
                "کد را با خط تیره بفرستید، مثل 1-2-3-4-5 (تلگرام کدی را که بدون جداکننده در چت بیاید گاهی باطل می‌کند).\n\n"
                "اگر کد نرسید، «ارسال مجدد کد» را بزنید یا با Session String وارد شوید.")

    def code_buttons(self):
        return [[Button.inline("🔁 ارسال مجدد کد", b"resend")],
                [Button.inline("🔑 ورود با Session String", b"sess")], CANCEL]

    async def save_setting(self, event, key, text):
        label, typ, lo, hi = SETTINGS[key]
        if typ is str:
            value = "" if text == "-" else text.strip()
            if value and not (value.startswith("@") or re.fullmatch(r"-?\d+", value)):
                await event.respond("یک @یوزرنیم، آیدی عددی یا - بفرستید.")
                return
        else:
            try:
                value = typ(text.replace(",", "."))
            except ValueError:
                await event.respond("یک عدد بفرستید.")
                return
            if not (lo <= value <= hi):
                await event.respond(f"عدد باید بین {lo} و {hi} باشد.")
                return
        await self.db.set(key, value)
        self.state = None
        await event.respond(f"✅ ذخیره شد: {label} = {value or '—'}", buttons=[[Button.inline("⚙️ تنظیمات", b"set")]])

    # ------------------------------------------------------------ دستورها
    async def cmd_route(self, event, text):
        body = text[len("/route"):].strip()
        if "=" not in body:
            await event.respond("روش استفاده:\n/route Plush Pepe = @channel\n/route Plush Pepe = @channel 5000\n(عدد آخر سقف قیمت به استارز است، اختیاری)")
            return
        name, rest = body.split("=", 1)
        parts = rest.split()
        if not parts:
            await event.respond("کانال را هم بنویسید.")
            return
        try:
            max_price = float(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            await event.respond("سقف قیمت باید عدد باشد.")
            return
        col = find_collection(await self.db.all_collections(), name)
        if not col:
            await event.respond(f"کالکشن پیدا نشد: {name.strip()}")
            return
        await self.db.set_route(col["id"], parts[0], max_price)
        await event.respond(f"✅ {col['title']} ← {parts[0]}" + (f" (سقف {max_price:,.0f} ⭐)" if max_price else ""))

    async def cmd_unroute(self, event, text):
        name = text[len("/unroute"):].strip()
        col = find_collection(await self.db.all_collections(), name)
        if not col:
            await event.respond(f"کالکشن پیدا نشد: {name}")
            return
        await self.db.set_route(col["id"], None, 0)
        await event.respond(f"✅ کانال {col['title']} حذف شد.")

    # ------------------------------------------------------------ دکمه‌ها
    async def on_callback(self, event):
        data = event.data.decode()
        await event.answer()

        if data == "menu":
            self.state = None
            await self.show(event, "🎁 ربات فلور گیفت — پنل مدیریت", menu_buttons())
        elif data == "cancel":
            self.state = None
            await self.mgr.login_cancel()
            await self.show(event, "🎁 ربات فلور گیفت — پنل مدیریت", menu_buttons())
        elif data == "noop":
            pass
        elif data == "acc":
            await self.view_accounts(event)
        elif data.startswith("acc:"):
            await self.view_account(event, int(data[4:]))
        elif data.startswith("add:"):
            role = data[4:]
            self.state = {"mode": "phone", "role": role}
            await self.show(event, "شماره‌ی اکانت را با کد کشور بفرستید، مثل +989121234567\n\n"
                                   "یا اگر کد به شماره نمی‌رسد، با Session String وارد شوید.",
                            [[Button.inline("🔑 ورود با Session String", f"sess:{role}".encode())], CANCEL])
        elif data == "resend":
            try:
                kind = await self.mgr.login_resend()
                await self.show(event, "کد دوباره ارسال شد.\n\n" + self.code_text(kind), self.code_buttons())
            except Exception as e:
                await self.show(event, f"ارسال مجدد انجام نشد: {type(e).__name__}", self.code_buttons())
        elif data == "sess" or data.startswith("sess:"):
            role = data[5:] if data.startswith("sess:") else (self.state or {}).get("role", "reader")
            await self.mgr.login_cancel()
            self.state = {"mode": "session", "role": role}
            await self.show(event, "Session String اکانت را بفرستید (پیام شما بلافاصله پاک می‌شود).\n\n"
                                   "برای ساختن آن فایل make_session.py را روی کامپیوتر خودتان اجرا کنید.", [CANCEL])
        elif data.startswith("act:"):
            await self.mgr.toggle(int(data[4:]))
            await self.view_accounts(event)
        elif data.startswith("rm:"):
            aid = data[3:]
            await self.show(event, f"اکانت #{aid} حذف شود؟ نشست آن هم بسته می‌شود.",
                            [[Button.inline("بله، حذف", f"rmy:{aid}".encode()), Button.inline("نه", b"acc")]])
        elif data.startswith("rmy:"):
            await self.mgr.remove(int(data[4:]))
            await self.view_accounts(event)
        elif data == "set":
            await self.view_settings(event)
        elif data.startswith("tog:"):
            key = data[4:]
            on = await self.db.get(key, "1") == "1"
            await self.db.set(key, "0" if on else "1")
            await self.view_settings(event)
        elif data.startswith("sv:"):
            key = data[3:]
            self.state = {"mode": "set", "key": key}
            await self.show(event, f"{SETTINGS[key][0]}\n\nمقدار جدید را بفرستید.", [CANCEL])
        elif data == "refresh":
            await self.show(event, "در حال دریافت لیست کالکشن‌ها از تلگرام…")
            st = await self.mgr.refresh_collections()
            await self.show(event, coll_report(st), [[Button.inline("😀 ایموجی‌ها", b"ep:0")], BACK])
        elif data.startswith("ep:"):
            self.state = None
            text, rows = await self.emoji_page(int(data[3:]))
            await self.show(event, text, rows)
        elif data.startswith("ec:"):
            _, cid, page = data.split(":")
            col = await self.db.collection(int(cid))
            self.state = {"mode": "emoji", "cid": int(cid), "page": int(page)}
            await self.show(event, f"ایموجی پریمیوم «{col['title']}» را بفرستید.\n"
                                   "(فقط خود ایموجی را بفرستید؛ کدش خودکار استخراج می‌شود.)",
                            [[Button.inline("« بازگشت به لیست", f"ep:{page}".encode())]])
        elif data == "chan":
            await self.view_channels(event)
        elif data == "status":
            await self.view_status(event)
        elif data == "check":
            await self.show(event, "در حال بررسی…")
            await self.show(event, await self.run_check(),
                            [[Button.inline("🧪 ارسال پیام تست به کانال کلی", b"test")], BACK])
        elif data == "test":
            ch = (await self.db.get("general_channel", "")) or ""
            if not ch:
                await self.show(event, "اول کانال کلی را در تنظیمات مشخص کنید.", [BACK])
                return
            err = await self.sender.send_test(ch)
            await self.show(event, f"✅ پیام تست به {ch} ارسال شد." if not err else f"❌ ارسال نشد:\n{err}", [BACK])

    # ------------------------------------------------------------ نماها
    async def view_accounts(self, event):
        accs = await self.db.accounts()
        rows = []
        for a in accs:
            icon = "🟢" if a["status"] == "ready" and a["enabled"] else "⚪️" if not a["enabled"] else "🔴"
            rows.append([Button.inline(f"{icon} #{a['id']} {a['phone']} · {ROLE_FA.get(a['role'], a['role'])}",
                                       f"acc:{a['id']}".encode())])
        rows.append([Button.inline("➕ اسکنر", b"add:reader"), Button.inline("➕ ارسال‌کننده (پریمیوم)", b"add:sender")])
        rows.append(BACK)
        text = ("👥 اکانت‌ها\n\n"
                "اسکنرها مارکت را می‌خوانند (پریمیوم لازم نیست).\n"
                "ارسال‌کننده پست‌ها را با ایموجی پریمیوم در کانال می‌فرستد (اکانت پریمیوم خودتان).")
        if not accs:
            text += "\n\nهنوز اکانتی اضافه نشده."
        await self.show(event, text, rows)

    async def view_account(self, event, aid):
        a = await self.db.account(aid)
        if not a:
            return await self.view_accounts(event)
        text = (f"#{a['id']} {a['phone']}\nنقش: {ROLE_FA.get(a['role'], a['role'])}\n"
                f"وضعیت: {STATUS_FA.get(a['status'], a['status'])}\nروشن: {'بله' if a['enabled'] else 'خیر'}")
        await self.show(event, text, [
            [Button.inline("⏸ خاموش" if a["enabled"] else "▶️ روشن", f"act:{aid}".encode()),
             Button.inline("🗑 حذف", f"rm:{aid}".encode())],
            [Button.inline("« بازگشت", b"acc")],
        ])

    async def view_settings(self, event):
        s = await self.db.all_settings()
        on, ton = s["enabled"] == "1", s.get("ton_enabled", "1") == "1"
        text = (
            "⚙️ تنظیمات\n\n"
            f"اسکنر: {'روشن' if on else 'متوقف'}\n"
            f"فاصله‌ی بررسی: {s['interval']} ثانیه برای هر کالکشن\n"
            f"حداقل مکث هر اکانت: {s['min_delay']} ثانیه\n"
            f"درخواست هم‌زمان هر اسکنر: {s['concurrency']}\n"
            f"کم‌تحرک‌ها {s['cold_factor']} برابر کندتر بررسی می‌شوند\n"
            f"کانال کلی: {s['general_channel'] or '—'}\n"
            f"حداقل تخفیف زیر فلور: {s['min_discount']}٪\n"
            f"سقف قیمت کانال کلی: {s['max_price']} ⭐ (0 = بدون محدودیت)\n"
            f"گیفت‌های لیست‌شده با TON: {'ارسال می‌شود' if ton else 'ارسال نمی‌شود'}"
        )
        await self.show(event, text, [
            [Button.inline("⏱ فاصله‌ی بررسی", b"sv:interval"), Button.inline("🐢 حداقل مکث", b"sv:min_delay")],
            [Button.inline("⚡️ هم‌زمانی", b"sv:concurrency"), Button.inline("🧊 ضریب کم‌تحرک", b"sv:cold_factor")],
            [Button.inline("📣 کانال کلی", b"sv:general_channel")],
            [Button.inline("📉 حداقل تخفیف", b"sv:min_discount"), Button.inline("💰 سقف قیمت", b"sv:max_price")],
            [Button.inline("💎 TON: خاموش کن" if ton else "💎 TON: روشن کن", b"tog:ton_enabled")],
            [Button.inline("⏸ توقف اسکنر" if on else "▶️ ادامه‌ی اسکنر", b"tog:enabled")],
            BACK,
        ])

    async def emoji_page(self, page):
        cols = [c for c in await self.db.all_collections() if c["resale"] > 0]
        if not cols:
            st = self.mgr.coll_status
            return ("هنوز لیست کالکشن‌ها خالی است.\n\n" + coll_report(st),
                    [[Button.inline("🔄 دریافت دوباره‌ی لیست", b"refresh")], BACK])
        per = 12
        pages = max(1, math.ceil(len(cols) / per))
        page = max(0, min(page, pages - 1))
        chunk = cols[page * per:(page + 1) * per]
        rows = []
        for i in range(0, len(chunk), 2):
            rows.append([
                Button.inline(("✅ " if c["emoji_id"] else "▫️ ") + c["title"][:22], f"ec:{c['id']}:{page}".encode())
                for c in chunk[i:i + 2]
            ])
        nav = []
        if page > 0:
            nav.append(Button.inline("◀️", f"ep:{page - 1}".encode()))
        nav.append(Button.inline(f"{page + 1}/{pages}", b"noop"))
        if page < pages - 1:
            nav.append(Button.inline("▶️", f"ep:{page + 1}".encode()))
        rows.append(nav)
        rows.append(BACK)
        done = sum(1 for c in cols if c["emoji_id"])
        text = (f"😀 ایموجی کالکشن‌ها\n\n{done} از {len(cols)} کالکشن ایموجی دارند.\n"
                "روی کالکشن بزنید، بعد ایموجی پریمیوم آن را بفرستید.")
        return text, rows

    async def view_channels(self, event):
        cols = [c for c in await self.db.all_collections() if c["channel"]]
        s = await self.db.all_settings()
        text = f"📡 کانال‌ها\n\nکانال کلی: {s['general_channel'] or '—'}\n\n"
        text += "\n".join(
            f"{c['title']} ← {c['channel']}" + (f" (سقف {c['max_price']:,.0f} ⭐)" if c["max_price"] else "")
            for c in cols
        ) or "هنوز کانال جداگانه‌ای برای کالکشن‌ها تنظیم نشده."
        text += ("\n\nدستورها:\n/route Plush Pepe = @channel [سقف قیمت]\n/unroute Plush Pepe\n\n"
                 "اکانت ارسال‌کننده باید ادمین همه‌ی کانال‌ها باشد.")
        await self.show(event, text, [BACK])

    async def view_status(self, event):
        s = await self.db.all_settings()
        cols = await self.db.active_collections()
        readers = self.mgr.ready_readers()
        accs = await self.db.accounts()
        lines = [
            "📊 وضعیت",
            f"اسکنر: {'روشن' if s['enabled'] == '1' else 'متوقف'}",
            f"اسکنرهای آماده: {len(readers)} از {sum(1 for a in accs if a['role'] == 'reader')}",
            f"ارسال‌کننده: {'آماده' if self.mgr.sender_client() else 'وصل نیست'}",
            f"کالکشن‌های دارای لیست: {len(cols)}",
            ("آخرین دریافت لیست: " + ("خطا — " + self.mgr.coll_status["error"] if self.mgr.coll_status.get("error") else "موفق"))
            if self.mgr.coll_status.get("checked") else "آخرین دریافت لیست: هنوز انجام نشده",
            f"بررسی‌ها: {self.mgr.stats['polls']} · هشدارها: {self.mgr.stats['alerts']} · ارسال‌شده: {self.sender.sent}",
            f"خطاها: {self.mgr.stats['errors']} · صف: {self.mgr.queue.qsize()}",
        ]
        if cols and readers:
            interval, min_delay = float(s["interval"]), float(s["min_delay"])
            cold_factor = float(s.get("cold_factor", "3"))
            hot, cold, need = self.mgr.load(interval, cold_factor)
            cap = len(readers) / min_delay
            lines.append(f"کالکشن‌های پرتحرک: {hot} · کم‌تحرک: {cold}")
            lines.append(f"ظرفیت: {cap:.1f} درخواست بر ثانیه · نیاز برای هدف {interval:g} ثانیه: {need:.1f}")
            if need > cap * 0.85:
                want = math.ceil(need * min_delay / 0.85)
                lines.append(f"⚠️ ظرفیت کم است. برای رسیدن به هدف حدود {want} اسکنر لازم است (الان {len(readers)} تا).")
            else:
                lines.append("✅ ظرفیت کافی است.")
        await self.show(event, "\n".join(lines), [BACK])

    async def run_check(self):
        import telethon
        from telethon.tl.alltlobjects import LAYER

        lines = [f"Telethon {telethon.__version__} (layer {LAYER})"]
        for name in ("GetStarGiftsRequest", "GetResaleStarGiftsRequest"):
            lines.append(("✅ " if hasattr(functions.payments, name) else "❌ ") + name)
        client = self.mgr.any_client()
        if not client:
            lines.append("هیچ اکانت لاگین‌شده‌ای برای تست نیست.")
        else:
            try:
                cols, stats = await market.fetch_collections(client)
                lines.append(coll_report({"checked": True, "error": None, "count": len(cols), "stats": stats}))
                if cols:
                    ls = await market.fetch_listings(client, cols[0]["id"], 3)
                    lines.append(f"✅ لیست‌های خوانده‌شده از {cols[0]['title']}: {len(ls)}")
                    if ls:
                        l = ls[0]
                        lines.append(f"نمونه: #{l['num']} {l['amount']} {l['cur']} · {l['model'][0]} / {l['backdrop'][0]} / {l['symbol'][0]}")
                        lines.append(f"جزئیات قیمت: {l['dbg']}")
            except Exception as e:
                lines.append(f"❌ {type(e).__name__}: {e}")
        return "\n".join(lines)
