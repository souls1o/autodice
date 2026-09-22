"""Persistent registry of controlled deposit addresses that may hold funds."""

from __future__ import annotations

from datetime import datetime

from wallet.hd import CHANGE_INDEX, fingerprint, ticket_index

_collection = None


def _col():
    global _collection
    if _collection is None:
        from services import db

        _collection = db.wallet_registry
    return _collection


def _fp():
    try:
        return fingerprint()
    except Exception:
        return "unconfigured"


async def register_address(coin: str, address: str, index: int, *, channel_id=None, change=False):
    coin = (coin or "").lower()
    address = str(address or "").strip()
    if not address:
        return
    doc = {
        "fp": _fp(),
        "coin": coin,
        "address": address,
        "index": int(index),
        "change": bool(change),
        "channel_id": int(channel_id) if channel_id is not None else None,
        "updated_at": datetime.utcnow(),
    }
    await _col().update_one(
        {"fp": doc["fp"], "coin": coin, "address": address},
        {"$set": doc, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )


async def register_ticket_address(coin: str, address: str, channel_id: int):
    idx = ticket_index(channel_id)
    await register_address(coin, address, idx, channel_id=channel_id, change=False)


async def ensure_change_registered(coin: str):
    from wallet.hd import address_for

    coin = (coin or "").lower()
    addr = address_for(coin, CHANGE_INDEX, change=(coin == "ltc"))
    await register_address(coin, addr, CHANGE_INDEX, change=(coin == "ltc"))
    return addr


async def list_registered(coin: str) -> list[dict]:
    coin = (coin or "").lower()
    cursor = _col().find({"fp": _fp(), "coin": coin})
    return await cursor.to_list(length=50_000)


async def unregister_address(coin: str, address: str):
    await _col().delete_one({"fp": _fp(), "coin": (coin or "").lower(), "address": str(address).strip()})


async def prune_zero_usd(coin: str, address: str, balance_coin: float, price_usd: float, *, pending: bool = False):
    """Remove from spend registry when confirmed balance is $0.00 and no pending inbound."""
    if pending:
        return False
    try:
        bal = float(balance_coin or 0)
    except (TypeError, ValueError):
        return False
    if bal <= 0:
        await unregister_address(coin, address)
        return True
    try:
        price = float(price_usd or 0)
    except (TypeError, ValueError):
        return False
    # Unknown price must not wipe funded keys from the spend set.
    if price <= 0:
        return False
    usd = round(bal * price, 2)
    if usd > 0:
        return False
    await unregister_address(coin, address)
    return True
