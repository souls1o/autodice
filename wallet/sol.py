"""Solana: BIP44 addresses, balance/signatures, multi-instruction send."""

from __future__ import annotations

import asyncio
import json

import requests
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from wallet import config as wconfig
from wallet.hd import derive_sol
from wallet.select import select_largest_first

UNITS = 1_000_000_000  # lamports
FEE_LAMPORTS = 5000


def _rpc(method: str, params: list):
    url = wconfig.sol_rpc_url()
    if not url:
        raise RuntimeError("SOL_RPC_URL is not configured")
    r = requests.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data.get("result")


async def get_balance(address: str) -> float | None:
    try:
        def _bal():
            res = _rpc("getBalance", [address])
            lamports = int((res or {}).get("value") or 0)
            return lamports / UNITS

        return await asyncio.to_thread(_bal)
    except Exception as exc:
        print(f"[wallet.sol] balance failed {address}: {exc}")
        return None


async def get_receipts(address: str) -> list[dict] | None:
    """Use balance-delta polling (signature parse without amount is unreliable)."""
    return None


def _keypair_for_index(index: int) -> Keypair:
    _addr, secret = derive_sol(index)
    if len(secret) == 64:
        return Keypair.from_bytes(secret)
    return Keypair.from_seed(secret[:32])


def _transfer_sync(dest: str, amount_lamports: int, selected: list[dict]) -> dict:
    dest_pk = Pubkey.from_string(dest)
    # fee payer = largest selected
    selected = sorted(selected, key=lambda x: int(x["amount"]), reverse=True)
    signers = []
    ixs = []
    remaining = int(amount_lamports)
    for i, entry in enumerate(selected):
        kp = _keypair_for_index(int(entry["index"]))
        signers.append(kp)
        bal = int(entry["amount"])
        # reserve fee on fee payer
        if i == 0:
            usable = max(bal - FEE_LAMPORTS, 0)
        else:
            usable = bal
        send_amt = min(usable, remaining)
        if send_amt <= 0:
            continue
        ixs.append(
            transfer(
                TransferParams(
                    from_pubkey=kp.pubkey(),
                    to_pubkey=dest_pk,
                    lamports=int(send_amt),
                )
            )
        )
        remaining -= send_amt
        if remaining <= 0:
            break
    if remaining > 0:
        return {"error": "insufficient SOL after fee reserve"}
    if not ixs:
        return {"error": "nothing to send"}

    fee_payer = signers[0]
    bh = _rpc("getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = bh["value"]["blockhash"]
    from solders.hash import Hash
    from solders.message import Message

    msg = Message.new_with_blockhash(ixs, fee_payer.pubkey(), Hash.from_string(blockhash))
    tx = Transaction.new_unsigned(msg)
    tx.sign(signers, Hash.from_string(blockhash))
    raw = bytes(tx)
    import base64

    sig = _rpc("sendTransaction", [base64.b64encode(raw).decode("ascii"), {"encoding": "base64"}])
    return {"ok": True, "txid": sig}


async def transfer(dest: str, amount_lamports: int, funded: list[dict]) -> dict:
    dest = (dest or "").strip()
    if not dest or amount_lamports <= 0:
        return {"error": "invalid destination or amount"}

    entries = []
    for f in funded:
        lamports = int(f.get("balance_lamports") or 0)
        if lamports <= 0:
            continue
        entries.append(
            {
                "address": f["address"],
                "index": int(f["index"]),
                "amount": lamports,
            }
        )
    need = amount_lamports + FEE_LAMPORTS
    selected = select_largest_first(entries, need)
    if selected is None:
        return {"error": "insufficient SOL balance"}
    try:
        return await asyncio.to_thread(_transfer_sync, dest, amount_lamports, selected)
    except Exception as exc:
        print(f"[wallet.sol] transfer failed: {exc}")
        return {"error": str(exc)}
