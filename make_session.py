"""Run this on your own computer to log an account in and print a session string.

    pip install telethon
    python make_session.py

Then in the bot: Accounts -> Add -> "Login with session string" and paste the result.
Never share the string with anyone else: it is full access to the account.
"""
import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(input("API_ID: ").strip())
    api_hash = input("API_HASH: ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()  # asks for phone, code and password if needed
    print("\nSESSION STRING:\n")
    print(client.session.save())
    await client.disconnect()


asyncio.run(main())
