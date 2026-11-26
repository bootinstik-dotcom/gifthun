import os


def _need(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing environment variable: {name}")
    return value


API_ID = int(_need("API_ID"))
API_HASH = _need("API_HASH")
BOT_TOKEN = _need("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "858897695"))

# On Railway attach a Volume mounted at /data so the database survives deploys.
DB_PATH = os.getenv("DB_PATH") or ("/data/bot.db" if os.path.isdir("/data") else "bot.db")

DEFAULTS = {
    "enabled": "1",
    "interval": "3",          # seconds between two checks of the same collection
    "min_delay": "0.25",      # minimum pause between two API calls of one account
    "general_channel": "@NFTTrackerIr",
    "min_discount": "0",      # % below previous floor, general channel only
    "max_price": "0",         # stars, general channel only, 0 = no limit
}
