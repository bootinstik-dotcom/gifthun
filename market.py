"""Reads Telegram's gift resale market through the raw MTProto API."""
from telethon import functions


async def fetch_collections(client):
    """Collections that currently have at least one resale listing."""
    res = await client(functions.payments.GetStarGiftsRequest(hash=0))
    out = []
    for g in getattr(res, "gifts", None) or []:
        resale = getattr(g, "availability_resale", 0) or 0
        if resale <= 0:
            continue
        title = getattr(g, "title", None) or f"Gift {g.id}"
        out.append({"id": g.id, "title": title, "resale": int(resale)})
    return out


def _price(g):
    """Returns (currency, amount) or None. Handles Stars and TON listings."""
    amounts = getattr(g, "resell_amount", None)
    if amounts:
        if not isinstance(amounts, (list, tuple)):
            amounts = [amounts]
        picked = None
        for a in amounts:
            if "Ton" not in type(a).__name__:
                picked = ("XTR", float(getattr(a, "amount", 0)))
                break
        if picked is None:
            picked = ("TON", float(getattr(amounts[0], "amount", 0)) / 1e9)
        return picked
    stars = getattr(g, "resell_stars", None)
    if stars is not None:
        return ("XTR", float(stars))
    return None


def parse_listing(g):
    price = _price(g)
    if not price:
        return None
    attrs = {"model": (None, None), "backdrop": (None, None), "symbol": (None, None)}
    for a in getattr(g, "attributes", None) or []:
        kind = type(a).__name__
        val = (getattr(a, "name", None), getattr(a, "rarity_permille", None))
        if kind.endswith("Model"):
            attrs["model"] = val
        elif kind.endswith("Backdrop"):
            attrs["backdrop"] = val
        elif kind.endswith("Pattern"):
            attrs["symbol"] = val
    return {
        "uid": getattr(g, "id", None),
        "slug": getattr(g, "slug", None),
        "num": getattr(g, "num", None),
        "title": getattr(g, "title", None),
        "cur": price[0],
        "amount": price[1],
        **attrs,
    }


async def fetch_listings(client, gift_id, limit=20):
    """Cheapest listings of one collection."""
    res = await client(
        functions.payments.GetResaleStarGiftsRequest(
            gift_id=gift_id, offset="", limit=limit, sort_by_price=True
        )
    )
    out = []
    for g in getattr(res, "gifts", None) or []:
        item = parse_listing(g)
        if item and item["uid"] is not None:
            out.append(item)
    return out
