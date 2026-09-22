"""Litecoin: BIP84 addresses, balances/receipts, multi-input send via API + local WIF sign."""

from __future__ import annotations

import asyncio
import hashlib

import requests
from coincurve import PrivateKey as CCPrivateKey

from wallet import config as wconfig
from wallet.hd import CHANGE_INDEX, derive_ltc
from wallet.select import select_largest_first

UNITS = 100_000_000
DUST = 546


def _api_base():
    return (wconfig.ltc_api_url() or "https://api.blockcypher.com/v1/ltc/main").rstrip("/")


def _params():
    token = wconfig.ltc_api_token()
    return {"token": token} if token else {}


def _get(path, **extra):
    r = requests.get(f"{_api_base()}{path}", params={**_params(), **extra}, timeout=20)
    r.raise_for_status()
    return r.json()


def _post(path, payload):
    r = requests.post(f"{_api_base()}{path}", params=_params(), json=payload, timeout=45)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"LTC API {r.status_code}: {detail}")
    return r.json()


async def get_balance(address: str) -> float | None:
    try:
        data = await asyncio.to_thread(_get, f"/addrs/{address}/balance")
        bal = int(data.get("balance") or 0) + int(data.get("unconfirmed_balance") or 0)
        return bal / UNITS
    except Exception as exc:
        print(f"[wallet.ltc] balance failed {address}: {exc}")
        return None


async def get_receipts(address: str) -> list[dict] | None:
    try:
        data = await asyncio.to_thread(_get, f"/addrs/{address}", limit=30)
        out = []
        seen = set()
        for ref in list(data.get("txrefs") or []) + list(data.get("unconfirmed_txrefs") or []):
            if int(ref.get("tx_output_n", -1)) < 0:
                continue
            txid = str(ref.get("tx_hash") or "")
            vout = ref.get("tx_output_n")
            rid = f"{txid}:{vout}"
            if not txid or rid in seen:
                continue
            seen.add(rid)
            amt = int(ref.get("value") or 0)
            if amt <= 0:
                continue
            out.append({"id": rid, "type": "receipt", "amount": amt})
        return out
    except Exception as exc:
        print(f"[wallet.ltc] receipts failed {address}: {exc}")
        return None


def _utxos_sync(address: str) -> list[dict]:
    data = _get(f"/addrs/{address}", unspentOnly="true")
    refs = list(data.get("txrefs") or []) + list(data.get("unconfirmed_txrefs") or [])
    out = []
    for ref in refs:
        if int(ref.get("tx_output_n", -1)) < 0:
            continue
        out.append(
            {
                "txid": ref["tx_hash"],
                "vout": int(ref["tx_output_n"]),
                "amount": int(ref.get("value") or 0),
                "address": address,
            }
        )
    return out


async def list_utxos(address: str) -> list[dict]:
    try:
        return await asyncio.to_thread(_utxos_sync, address)
    except Exception as exc:
        print(f"[wallet.ltc] utxo failed {address}: {exc}")
        return []


def _wif_to_privbytes(wif: str) -> bytes:
    # bip_utils WIF → use coincurve via decoded raw from derive (prefer raw)
    import base58

    raw = base58.b58decode_check(wif)
    # prefix + 32 byte key (+ optional 0x01 compressed)
    key = raw[1:33]
    return key


def _sign_tosign(tosign_hex: str, wif: str) -> str:
    priv = _wif_to_privbytes(wif)
    sk = CCPrivateKey(priv)
    digest = bytes.fromhex(tosign_hex)
    # BlockCypher expects DER signature; coincurve sign returns 64-byte compact — use der
    sig = sk.sign(digest, hasher=None)
    return sig.hex()


def _broadcast_selected(selected: list[dict], dest: str, amount_sats: int, change_addr: str, change_amt: int):
    # Pin exact UTXOs so coin-selection matches what we sign (multi-address).
    outputs = [{"addresses": [dest], "value": int(amount_sats)}]
    if change_amt >= DUST:
        outputs.append({"addresses": [change_addr], "value": int(change_amt)})

    stub = _post(
        "/txs/new",
        {
            "inputs": [
                {"prev_hash": u["txid"], "output_index": int(u["vout"])}
                for u in selected
            ],
            "outputs": outputs,
            "preference": "medium",
        },
    )
    if stub.get("errors"):
        raise RuntimeError(str(stub["errors"]))

    tosign = stub.get("tosign") or []
    # Map each tosign index to a private key — BlockCypher lists pubkeys
    pubkeys = stub.get("pubkeys") or []
    signatures = []
    # Build address→wif map
    addr_wif = {u["address"]: u["wif"] for u in selected}
    # For each tosign, BlockCypher also returns addresses in tx.inputs
    tx = stub.get("tx") or {}
    tx_inputs = tx.get("inputs") or []
    for i, digest in enumerate(tosign):
        addr = None
        if i < len(tx_inputs):
            addrs = tx_inputs[i].get("addresses") or []
            addr = addrs[0] if addrs else None
        wif = addr_wif.get(addr) if addr else None
        if not wif:
            # fallback: any wif (single-addr case)
            wif = next(iter(addr_wif.values()))
        signatures.append(_sign_tosign(digest, wif))

    stub["signatures"] = signatures
    # Provide pubkeys if missing — derive from WIF
    if not pubkeys or len(pubkeys) != len(signatures):
        stub["pubkeys"] = []
        for i, digest in enumerate(tosign):
            addr = None
            if i < len(tx_inputs):
                addrs = tx_inputs[i].get("addresses") or []
                addr = addrs[0] if addrs else None
            wif = addr_wif.get(addr) if addr else next(iter(addr_wif.values()))
            priv = _wif_to_privbytes(wif)
            stub["pubkeys"].append(CCPrivateKey(priv).public_key.format(compressed=True).hex())

    sent = _post("/txs/send", stub)
    txid = ((sent.get("tx") or {}).get("hash")) or sent.get("hash")
    return {"ok": True, "txid": txid}


async def transfer(dest: str, amount_sats: int, funded: list[dict]) -> dict:
    dest = (dest or "").strip()
    if not dest or amount_sats <= 0:
        return {"error": "invalid destination or amount"}

    all_utxos = []
    for entry in funded:
        addr = entry["address"]
        idx = int(entry["index"])
        is_change = bool(entry.get("change"))
        try:
            _a, wif, _p, _pub = derive_ltc(idx, change=is_change)
        except Exception as exc:
            return {"error": f"derive failed: {exc}"}
        for u in await list_utxos(addr):
            u["wif"] = wif
            all_utxos.append(u)

    if not all_utxos:
        return {"error": "no UTXOs available"}

    # Fee approx: BlockCypher fills fees when using /txs/new — overshoot selection
    fee_pad = 2000
    selected = select_largest_first(all_utxos, amount_sats + fee_pad)
    if selected is None:
        return {"error": "insufficient LTC balance"}

    total_in = sum(int(u["amount"]) for u in selected)
    # Leave headroom for network fee deducted by API; change computed after
    # Re-request with exact outputs; API subtracts fee from change if we use preference
    change_addr, _, _, _ = derive_ltc(CHANGE_INDEX, change=True)
    from wallet.registry import ensure_change_registered

    await ensure_change_registered("ltc")

    # Let BlockCypher decide fee: send amount to dest, rest change (API may adjust)
    change_guess = max(total_in - amount_sats - fee_pad, 0)

    try:
        return await asyncio.to_thread(
            _broadcast_selected,
            selected,
            dest,
            amount_sats,
            change_addr,
            change_guess,
        )
    except Exception as exc:
        print(f"[wallet.ltc] transfer failed: {exc}")
        return {"error": str(exc)}
