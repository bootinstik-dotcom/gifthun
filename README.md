# Gift Floor Bot

Scans Telegram's gift resale market and posts every listing priced at or below the collection floor
to a Telegram channel (premium emoji supported). Accounts, settings, emojis and channels are all
managed from the bot's own admin panel.

## How it works
- **Reader accounts** (any account, Premium not needed) scan the market. Collections are split between them automatically.
- **Sender account** (your Premium account) posts to the channels, so premium emoji work. It must be an admin of each channel.
- **The bot** is only the admin panel (`/start`).

## Deploy on Railway
1. Push these files to a GitHub repo and create a Railway project from it.
2. Add a **Volume** to the service, mount path `/data` (keeps the database and sessions).
3. Variables: `API_ID`, `API_HASH` (from my.telegram.org), `BOT_TOKEN`, `ADMIN_ID`.
4. Deploy, open the bot, send `/start`.

## Adding accounts
Panel: Accounts -> Add. Login by phone number (the code is shown with its delivery method, with a Resend button),
or by session string: run `python make_session.py` on your own computer and paste the result into the bot.
Session strings are full access to the account. Never share them.

## First setup (inside the bot)
1. **Accounts → Add Sender**: your Premium account (phone, then the code typed like `1-2-3-4-5`, then the 2FA password if any).
2. **Accounts → Add Reader**: add 3–5 accounts to start.
3. Make the sender an admin of your channel, then set the channel in **Settings → General channel**.
4. **Emojis**: tap a collection, then send its premium emoji. The code is extracted automatically.
5. **Check**: confirms the installed Telethon supports the gift market calls and reads a sample listing, and can post a test message to the general channel.

## Per-collection channels
`/route Plush Pepe = @channel` or `/route Plush Pepe = @channel 5000` (max price in stars). `/unroute Plush Pepe` removes it.

## Rules
- Only listings at least `min discount` % (default 1.5) below the previous floor are posted.
- Listings priced in TON are posted with a "Listed in TON" line (can be switched off in Settings).
- Alerts are served round-robin across collections, so one busy collection cannot flood the channel.

- All reader accounts share one schedule: the most overdue collection is checked next, so adding accounts speeds everything up.
- Quiet collections (nothing changed for 30 min) are checked less often (Settings -> quiet factor).
- If the build fails on `cryptg`, delete that line from requirements.txt (it only makes the bot faster).

## Notes
- The first scan of each collection only records current listings. Alerts start from the second scan.
- Status shows how long one full scan takes. If it is slower than your interval, add reader accounts.
- Never publish `.env`, the database or session strings.
