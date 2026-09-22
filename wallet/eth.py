"""Ethereum: BIP44 addresses, balance/receipts, consolidate-then-send."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from eth_account import Account
from web3 import Web3

from wallet import config as wconfig
from wallet.hd import CHANGE_INDEX, derive_eth
from wallet.select import select_largest_first

UNITS = 10**18

_w3_lock = None
_w3_cached = None


def _w3() -> Web3:
    """Reuse one HTTP provider — is_connected() every call was a major latency hit."""
    global _w3_cached
    if _w3_cached is not None:
        return _w3_cached
    url = wconfig.eth_rpc_url()
    if not url:
        raise RuntimeError("ETH_RPC_URL is not configured")
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 12}))
    _w3_cached = w3
    return w3


async def get_balance(address: str) -> float | None:
    try:
        def _bal():
            w3 = _w3()
            wei = w3.eth.get_balance(Web3.to_checksum_address(address))
            return float(Decimal(wei) / Decimal(UNITS))

        return await asyncio.to_thread(_bal)
    except Exception as exc:
        print(f"[wallet.eth] balance failed {address}: {exc}")
        return None


async def get_receipts(address: str) -> list[dict] | None:
    """
    Best-effort incoming native transfers via recent blocks is heavy.
    Return None so the poller falls back to balance-delta detection.
    """
    return None


def _send_wei_sync(from_index: int, dest: str, wei: int) -> dict:
    w3 = _w3()
    addr, priv = derive_eth(from_index)
    acct = Account.from_key(priv)
    if acct.address.lower() != addr.lower():
        return {"error": "key/address mismatch"}
    dest = Web3.to_checksum_address(dest)
    nonce = w3.eth.get_transaction_count(acct.address)
    gas_price = w3.eth.gas_price
    tx = {
        "to": dest,
        "value": int(wei),
        "nonce": nonce,
        "gas": 21000,
        "gasPrice": gas_price,
        "chainId": w3.eth.chain_id,
    }
    # Ensure balance covers gas
    bal = w3.eth.get_balance(acct.address)
    cost = wei + gas_price * 21000
    if bal < cost:
        return {"error": "insufficient ETH for amount+gas"}
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    txh = w3.eth.send_raw_transaction(raw)
    return {"ok": True, "txid": txh.hex()}


async def _send_wei(from_index: int, dest: str, wei: int) -> dict:
    try:
        return await asyncio.to_thread(_send_wei_sync, from_index, dest, wei)
    except Exception as exc:
        return {"error": str(exc)}


async def transfer(dest: str, amount_wei: int, funded: list[dict]) -> dict:
    """
    Prefer one address with enough balance; otherwise consolidate into richest, then pay.
    funded: [{address, index, balance_wei}, ...]
    """
    dest = (dest or "").strip()
    if not dest or amount_wei <= 0:
        return {"error": "invalid destination or amount"}

    # Estimate gas cushion
    try:
        w3 = await asyncio.to_thread(_w3)
        gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)
    except Exception as exc:
        return {"error": str(exc)}
    gas_cost = int(gas_price) * 21000

    entries = []
    for f in funded:
        bal = int(f.get("balance_wei") or 0)
        if bal <= 0:
            continue
        entries.append(
            {
                "address": f["address"],
                "index": int(f["index"]),
                "amount": bal,
            }
        )
    if not entries:
        return {"error": "no funded ETH addresses"}

    entries.sort(key=lambda x: x["amount"], reverse=True)

    # Single-address path
    for e in entries:
        if e["amount"] >= amount_wei + gas_cost:
            return await _send_wei(e["index"], dest, amount_wei)

    total = sum(e["amount"] for e in entries)
    if total < amount_wei + gas_cost:
        return {"error": "insufficient ETH balance"}

    # Consolidate into richest funded address
    target = entries[0]
    target_index = int(target["index"])
    for e in entries[1:]:
        # leave dust for gas on source
        sendable = e["amount"] - gas_cost
        if sendable <= 0:
            continue
        print(f"[wallet] eth consolidate {e['address']} -> {target['address']}")
        res = await _send_wei(e["index"], target["address"], sendable)
        if res.get("error"):
            return res

    # Refresh target balance via chain
    addr, _ = derive_eth(target_index)
    bal_eth = await get_balance(addr)
    if bal_eth is None:
        return {"error": "could not refresh ETH balance after consolidate"}
    bal_wei = int(bal_eth * UNITS)
    if bal_wei < amount_wei + gas_cost:
        return {"error": "insufficient ETH after consolidate"}
    return await _send_wei(target_index, dest, amount_wei)
