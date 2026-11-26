import asyncio
import logging
import time

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
from accounts import Manager
from db import DB
from panel import Panel
from sender import Sender

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


async def main():
    db = DB()
    await db.init()

    bot = TelegramClient(StringSession(await db.get("bot_session", "")), config.API_ID, config.API_HASH)
    await bot.start(bot_token=config.BOT_TOKEN)
    await db.set("bot_session", bot.session.save())

    last = {}

    async def notify(key, text, every=300):
        now = time.time()
        if now - last.get(key, 0) < every:
            return
        last[key] = now
        try:
            await bot.send_message(config.ADMIN_ID, text)
        except Exception:
            log.warning("could not notify admin: %s", text)

    mgr = Manager(db, notify)
    sender = Sender(mgr, db, notify)
    Panel(bot, db, mgr, sender).register()

    await mgr.start()
    asyncio.create_task(sender.run())
    log.info("Started. Open the bot and send /start")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
