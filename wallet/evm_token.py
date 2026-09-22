"""EVM ERC-20 / BEP-20 stablecoin balance + consolidate-then-send."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from eth_account import Account
from web3 import Web3

from wallet import config as wconfig
from wallet.hd import derive_eth
from wallet.tokens import token_info

_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


def _rpc_url(chain: str) -> str:
    if chain == "eth":
        url = wconfig.eth_rpc_url()
        if not url:
            raise RuntimeError("ETH_RPC_URL is not configured")
        return url
    if chain == "bnb":
        url = wconfig.bnb_rpc_url()
        if not url:
            raise RuntimeError("BNB_RPC_URL is not configured")
        return url
    raise RuntimeError(f"unsupported EVM chain: {chain}")


_w3_by_chain: dict[str, Web3] = {}


def _w3(chain: str) -> Web3:
    cached = _w3_by_chain.get(chain)
    if cached is not None:
        return cached
    w3 = Web3(Web3.HTTPProvider(_rpc_url(chain), request_kwargs={"timeout": 12}))
    _w3_by_chain[chain] = w3
    return w3


def _contract(w3: Web3, coin: str):
    info = token_info(coin)
    return w3.eth.contract(
        address=Web3.to_checksum_address(info["contract"]),
        abi=_ERC20_ABI,
    )


async def get_balance(address: str, coin: str) -> float | None:
    try:
        info = token_info(coin)
        chain = info["chain"]
        decimals = int(info["decimals"])

        def _bal():
            w3 = _w3(chain)
            c = _contract(w3, coin)
            raw = c.functions.balanceOf(Web3.to_checksum_address(address)).call()
            return float(Decimal(raw) / Decimal(10**decimals))

        return await asyncio.to_thread(_bal)
    except Exception as exc:
        print(f"[wallet.evm_token] balance failed {coin} {address}: {exc}")
        return None


async def get_receipts(address: str, coin: str) -> list[dict] | None:
    return None


def _send_token_sync(coin: str, from_index: int, dest: str, amount: int) -> dict:
    info = token_info(coin)
    chain = info["chain"]
    w3 = _w3(chain)
    addr, priv = derive_eth(from_index)
    acct = Account.from_key(priv)
    if acct.address.lower() != addr.lower():
        return {"error": "key/address mismatch"}
    dest = Web3.to_checksum_address(dest)
    c = _contract(w3, coin)
    nonce = w3.eth.get_transaction_count(acct.address)
    gas_price = w3.eth.gas_price
    # USDT on ETH sometimes lacks a bool return — build via encodeABI
    try:
        tx = c.functions.transfer(dest, int(amount)).build_transaction(
            {
                "from": acct.address,
                "nonce": nonce,
                "gasPrice": gas_price,
                "chainId": w3.eth.chain_id,
            }
        )
    except Exception:
        # Older USDT ABI quirks — manual calldata
        data = c.functions.transfer(dest, int(amount))._encode_transaction_data()
        tx = {
            "to": Web3.to_checksum_address(info["contract"]),
            "from": acct.address,
            "value": 0,
            "data": data,
            "nonce": nonce,
            "gasPrice": gas_price,
            "chainId": w3.eth.chain_id,
            "gas": 100_000,
        }
    if "gas" not in tx:
        try:
            tx["gas"] = w3.eth.estimate_gas(tx)
        except Exception:
            tx["gas"] = 100_000
    native = w3.eth.get_balance(acct.address)
    if native < tx["gas"] * gas_price:
        return {"error": f"insufficient {chain.upper()} for gas on {addr}"}
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    txh = w3.eth.send_raw_transaction(raw)
    return {"ok": True, "txid": txh.hex()}


async def _send_token(coin: str, from_index: int, dest: str, amount: int) -> dict:
    try:
        return await asyncio.to_thread(_send_token_sync, coin, from_index, dest, amount)
    except Exception as exc:
        return {"error": str(exc)}


async def transfer(coin: str, dest: str, amount: int, funded: list[dict]) -> dict:
    dest = (dest or "").strip()
    if not dest or amount <= 0:
        return {"error": "invalid destination or amount"}

    entries = []
    for f in funded:
        bal = int(f.get("balance_raw") or 0)
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
        return {"error": f"no funded {coin} addresses"}

    entries.sort(key=lambda x: x["amount"], reverse=True)

    for e in entries:
        if e["amount"] >= amount:
            return await _send_token(coin, e["index"], dest, amount)

    total = sum(e["amount"] for e in entries)
    if total < amount:
        return {"error": f"insufficient {coin} balance"}

    target = entries[0]
    for e in entries[1:]:
        if e["amount"] <= 0:
            continue
        print(f"[wallet] {coin} consolidate {e['address']} -> {target['address']}")
        res = await _send_token(coin, e["index"], target["address"], e["amount"])
        if res.get("error"):
            return res

    bal = await get_balance(target["address"], coin)
    if bal is None:
        return {"error": f"could not refresh {coin} after consolidate"}
    decimals = int(token_info(coin)["decimals"])
    raw = int(round(bal * (10**decimals)))
    if raw < amount:
        return {"error": f"insufficient {coin} after consolidate"}
    return await _send_token(coin, target["index"], dest, amount)
