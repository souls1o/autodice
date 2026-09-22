"""Solana SPL stablecoin (USDT/USDC) balance + multi-source send."""

from __future__ import annotations

import asyncio
import base64

import requests
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    TransferCheckedParams,
    create_associated_token_account,
    get_associated_token_address,
    transfer_checked,
)

from wallet import config as wconfig
from wallet.hd import derive_sol
from wallet.select import select_largest_first
from wallet.tokens import token_info

FEE_LAMPORTS = 10_000


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


def _keypair_for_index(index: int) -> Keypair:
    _addr, secret = derive_sol(index)
    if len(secret) == 64:
        return Keypair.from_bytes(secret)
    return Keypair.from_seed(secret[:32])


async def get_balance(address: str, coin: str) -> float | None:
    try:
        info = token_info(coin)
        mint = Pubkey.from_string(info["mint"])
        owner = Pubkey.from_string(address)
        ata = get_associated_token_address(owner, mint)
        decimals = int(info["decimals"])

        def _bal():
            res = _rpc("getTokenAccountBalance", [str(ata)])
            if not res or not res.get("value"):
                return 0.0
            return float(res["value"].get("uiAmount") or 0)

        return await asyncio.to_thread(_bal)
    except Exception as exc:
        # Missing ATA → zero balance
        msg = str(exc).lower()
        if "could not find" in msg or "invalid param" in msg or "not found" in msg:
            return 0.0
        print(f"[wallet.spl] balance failed {coin} {address}: {exc}")
        return None


async def get_receipts(address: str, coin: str) -> list[dict] | None:
    return None


def _ata_exists(ata: str) -> bool:
    res = _rpc("getAccountInfo", [ata, {"encoding": "base64"}])
    return bool(res and res.get("value"))


def _transfer_sync(coin: str, dest: str, amount: int, selected: list[dict]) -> dict:
    info = token_info(coin)
    mint = Pubkey.from_string(info["mint"])
    decimals = int(info["decimals"])
    dest_owner = Pubkey.from_string(dest)
    dest_ata = get_associated_token_address(dest_owner, mint)

    selected = sorted(selected, key=lambda x: int(x["amount"]), reverse=True)
    fee_payer = _keypair_for_index(int(selected[0]["index"]))
    ixs = []
    signers = [fee_payer]

    if not _ata_exists(str(dest_ata)):
        ixs.append(
            create_associated_token_account(
                fee_payer.pubkey(),
                dest_owner,
                mint,
            )
        )

    remaining = int(amount)
    for entry in selected:
        kp = _keypair_for_index(int(entry["index"]))
        if kp.pubkey() != fee_payer.pubkey():
            signers.append(kp)
        src_ata = get_associated_token_address(kp.pubkey(), mint)
        send_amt = min(int(entry["amount"]), remaining)
        if send_amt <= 0:
            continue
        ixs.append(
            transfer_checked(
                TransferCheckedParams(
                    program_id=TOKEN_PROGRAM_ID,
                    source=src_ata,
                    mint=mint,
                    dest=dest_ata,
                    owner=kp.pubkey(),
                    amount=int(send_amt),
                    decimals=decimals,
                )
            )
        )
        remaining -= send_amt
        if remaining <= 0:
            break
    if remaining > 0:
        return {"error": f"insufficient {coin} after selection"}
    if not ixs:
        return {"error": "nothing to send"}

    bh = _rpc("getLatestBlockhash", [{"commitment": "finalized"}])
    blockhash = bh["value"]["blockhash"]
    msg = Message.new_with_blockhash(ixs, fee_payer.pubkey(), Hash.from_string(blockhash))
    tx = Transaction.new_unsigned(msg)
    tx.sign(signers, Hash.from_string(blockhash))
    raw = bytes(tx)
    sig = _rpc(
        "sendTransaction",
        [base64.b64encode(raw).decode("ascii"), {"encoding": "base64"}],
    )
    return {"ok": True, "txid": sig}


async def transfer(coin: str, dest: str, amount: int, funded: list[dict]) -> dict:
    dest = (dest or "").strip()
    if not dest or amount <= 0:
        return {"error": "invalid destination or amount"}

    entries = []
    for f in funded:
        raw = int(f.get("balance_raw") or 0)
        if raw <= 0:
            continue
        entries.append(
            {
                "address": f["address"],
                "index": int(f["index"]),
                "amount": raw,
            }
        )
    selected = select_largest_first(entries, amount)
    if selected is None:
        return {"error": f"insufficient {coin} balance"}
    try:
        return await asyncio.to_thread(_transfer_sync, coin, dest, amount, selected)
    except Exception as exc:
        print(f"[wallet.spl] transfer failed: {exc}")
        return {"error": str(exc)}
