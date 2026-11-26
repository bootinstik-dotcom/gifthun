"""Reads Telegram's gift resale market through the raw MTProto API."""
from telethon import functions


async def fetch_collections(client):
    """Returns (collections that have resale listings, diagnostic stats)."""
    res = await client(functions.payments.GetStarGiftsRequest(hash=0))
    gifts = getattr(res, "gifts", None) or []
    stats = {"raw": len(gifts), "with_attr": 0, "listed": 0, "fields": [], "kind": type(res).__name__}
    if gifts:
        try:
            stats["fields"] = sorted(gifts[0].to_dict().keys())
        except Exception:
            pass
    out = []
    for g in gifts:
        resale = getattr(g, "availability_resale", None)
        if resale is not None:
            stats["with_attr"] += 1
        else:
            # this Telethon layer has no resale counter: fall back to every limited gift
            if not (getattr(g, "limited", False) or getattr(g, "availability_total", None)):
                continue
            resale = 1
        if resale <= 0:
            continue
        title = getattr(g, "title", None) or f"Gift {g.id}"
        out.append({"id": g.id, "title": title, "resale": int(resale)})
    stats["listed"] = len(out)
    return out, stats


def _price(g):
    """Returns (currency, amount, debug) or None.

    Gifts that can only be resold for TON carry a TON price; their Stars value is just an
    equivalent, so the currency must come from the TON flag, not from the first amount."""
    ton_only = any(getattr(g, n, False) for n in ("resale_ton_only", "resell_ton_only"))
    amounts = getattr(g, "resell_amount", None)
    if amounts is not None and not isinstance(amounts, (list, tuple)):
        amounts = [amounts]
    stars = ton = None
    for a in amounts or []:
        if "Ton" in type(a).__name__:
            ton = float(getattr(a, "amount", 0)) / 1e9
        else:
            stars = float(getattr(a, "amount", 0)) + float(getattr(a, "nanos", 0) or 0) / 1e9
    legacy = getattr(g, "resell_stars", None)
    if stars is None and legacy is not None:
        stars = float(legacy)
    dbg = f"ton_only={ton_only} amounts={[type(a).__name__ for a in amounts or []]}"
    if ton_only:
        return ("TON", ton, dbg) if ton is not None else None
    if stars is not None:
        return ("XTR", stars, dbg)
    if ton is not None:
        return ("TON", ton, dbg)
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
        "dbg": price[2],
        **attrs,
    }


async def fetch_listings(client, gift_id, limit=12):
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
