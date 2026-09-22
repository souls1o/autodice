"""Public wallet facade — drop-in for former Apirone helpers."""

from __future__ import annotations

from wallet.hd import CHANGE_INDEX, address_for, ticket_index
from wallet import eth as eth_mod
from wallet import evm_token as evm_token_mod
from wallet import ltc as ltc_mod
from wallet import sol as sol_mod
from wallet import spl_token as spl_token_mod
from wallet import registry
from wallet.tokens import STABLECOINS, is_stable, token_info


NATIVE = ("ltc", "eth", "sol")
SUPPORTED = NATIVE + tuple(sorted(STABLECOINS))


async def create_address(coin: str, *, channel_id: int | None = None) -> str | None:
    coin = (coin or "").lower()
    if coin not in SUPPORTED and coin not in STABLECOINS:
        return None
    try:
        if channel_id is None:
            idx = CHANGE_INDEX
            change = coin == "ltc"
            addr = address_for(coin, idx, change=change)
            await registry.register_address(coin, addr, idx, change=change)
            return addr
        idx = ticket_index(int(channel_id))
        addr = address_for(coin, idx, change=False)
        # Register companions so ETH/SOL ticket addrs can hold/spend stables too.
        from wallet.tokens import companion_coins

        for c in companion_coins(coin):
            await registry.register_ticket_address(c, addr, int(channel_id))
        return addr
    except Exception as exc:
        print(f"[wallet] create_address({coin}) failed: {exc}")
        return None


async def address_balance(address: str, coin: str = "ltc") -> float | None:
    coin = (coin or "ltc").lower()
    if coin == "ltc":
        return await ltc_mod.get_balance(address)
    if coin == "eth":
        return await eth_mod.get_balance(address)
    if coin == "sol":
        return await sol_mod.get_balance(address)
    if is_stable(coin):
        if token_info(coin)["chain"] == "sol":
            return await spl_token_mod.get_balance(address, coin)
        return await evm_token_mod.get_balance(address, coin)
    return None


async def address_receipts(address: str, coin: str = "ltc") -> list[dict] | None:
    coin = (coin or "ltc").lower()
    if coin == "ltc":
        return await ltc_mod.get_receipts(address)
    if coin == "eth":
        return await eth_mod.get_receipts(address)
    if coin == "sol":
        return await sol_mod.get_receipts(address)
    if is_stable(coin):
        if token_info(coin)["chain"] == "sol":
            return await spl_token_mod.get_receipts(address, coin)
        return await evm_token_mod.get_receipts(address, coin)
    return None


async def _funded_entries(coin: str) -> list[dict]:
    """Refresh balances, prune $0 USD addresses, return funded registry rows with balances."""
    from bets import STABLECOINS as BET_STABLES, get_price_async

    coin = (coin or "").lower()
    rows = await registry.list_registered(coin)
    if coin in BET_STABLES or is_stable(coin):
        price = 1.0
    else:
        try:
            price = float(await get_price_async(coin))
        except Exception:
            price = 0.0

    funded = []
    for row in rows:
        addr = row.get("address")
        if not addr:
            continue
        bal = await address_balance(addr, coin)
        if bal is None:
            continue
        pruned = await registry.prune_zero_usd(coin, addr, bal, price, pending=False)
        if pruned:
            continue
        if bal <= 0:
            continue
        entry = {
            "address": addr,
            "index": int(row.get("index") or 0),
            "change": bool(row.get("change")),
            "balance": float(bal),
        }
        if coin == "ltc":
            entry["balance_sats"] = int(round(bal * ltc_mod.UNITS))
        elif coin == "eth":
            entry["balance_wei"] = int(round(bal * eth_mod.UNITS))
        elif coin == "sol":
            entry["balance_lamports"] = int(round(bal * sol_mod.UNITS))
        elif is_stable(coin):
            decimals = int(token_info(coin)["decimals"])
            entry["balance_raw"] = int(round(bal * (10**decimals)))
        funded.append(entry)
    return funded


async def transfer(coin: str, dest: str, amount_smallest: int) -> dict:
    """
    Send `amount_smallest` of `coin` to `dest` using multi-address coin selection.
    Returns {ok: True, ...} or {error: "..."}.
    """
    coin = (coin or "").lower()
    try:
        amount_smallest = int(amount_smallest)
    except (TypeError, ValueError):
        return {"error": "invalid amount"}
    if amount_smallest <= 0:
        return {"error": "amount must be positive"}
    dest = (dest or "").strip()
    if not dest:
        return {"error": "missing destination"}

    await registry.ensure_change_registered(coin)
    funded = await _funded_entries(coin)
    if not funded:
        return {"error": f"no funded {coin} addresses in registry"}

    if coin == "ltc":
        return await ltc_mod.transfer(dest, amount_smallest, funded)
    if coin == "eth":
        return await eth_mod.transfer(dest, amount_smallest, funded)
    if coin == "sol":
        return await sol_mod.transfer(dest, amount_smallest, funded)
    if is_stable(coin):
        if token_info(coin)["chain"] == "sol":
            return await spl_token_mod.transfer(coin, dest, amount_smallest, funded)
        return await evm_token_mod.transfer(coin, dest, amount_smallest, funded)
    return {"error": f"unsupported coin: {coin}"}


async def account_balance() -> dict:
    """
    Aggregate balances across non-zero registered addresses.
    Shape compatible with former Apirone account balance reader:
    {"balance": [{"currency": "ltc", "total": <smallest>}, ...]}
    """
    out = []
    for coin in SUPPORTED:
        funded = await _funded_entries(coin)
        if coin == "ltc":
            total = sum(int(e.get("balance_sats") or 0) for e in funded)
        elif coin == "eth":
            total = sum(int(e.get("balance_wei") or 0) for e in funded)
        elif coin == "sol":
            total = sum(int(e.get("balance_lamports") or 0) for e in funded)
        elif is_stable(coin):
            total = sum(int(e.get("balance_raw") or 0) for e in funded)
        else:
            total = 0
        out.append({"currency": coin, "total": total})
    return {"balance": out}
