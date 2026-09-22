"""Litecoin: BIP84 P2WPKH — local coin-select/sign; Esplora-compatible chain RPC for UTXOs/broadcast."""

from __future__ import annotations

import asyncio
import hashlib
import struct

import requests
from coincurve import PrivateKey as CCPrivateKey

from wallet import config as wconfig
from wallet.hd import CHANGE_INDEX, derive_ltc
from wallet.select import select_largest_first

UNITS = 100_000_000
DUST = 546
# ~vbytes for P2WPKH: 10.5 overhead + 68/in + 31/out (segwit weight/4)
FEE_RATE_SAT_VB = 20


def _rpc_base() -> str:
    return (
        wconfig.ltc_rpc_url()
        or "https://litecoinspace.org/api"
    ).rstrip("/")


def _get(path: str):
    r = requests.get(f"{_rpc_base()}{path}", timeout=30)
    r.raise_for_status()
    if r.headers.get("content-type", "").startswith("application/json") or r.text[:1] in "{[":
        return r.json()
    return r.text


def _post_tx(raw_hex: str) -> str:
    r = requests.post(
        f"{_rpc_base()}/tx",
        data=raw_hex,
        headers={"Content-Type": "text/plain"},
        timeout=45,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"LTC broadcast {r.status_code}: {r.text}")
    return r.text.strip()


def _sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _hash256(b: bytes) -> bytes:
    return _sha256(_sha256(b))


def _hash160(b: bytes) -> bytes:
    return hashlib.new("ripemd160", _sha256(b)).digest()


def _varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def _bech32_decode(addr: str) -> tuple[int, bytes]:
    """Return (witver, program) for a bech32/bech32m address."""
    charset = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    addr = addr.strip()
    if addr != addr.lower() and addr != addr.upper():
        raise ValueError("invalid bech32 case")
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1:
        raise ValueError("invalid bech32")
    hrp, data = addr[:pos], addr[pos + 1 :]
    if hrp not in ("ltc", "tltc"):
        raise ValueError(f"unsupported hrp: {hrp}")
    try:
        values = [charset.index(c) for c in data]
    except ValueError as exc:
        raise ValueError("invalid bech32 charset") from exc

    def _polymod(vals):
        gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
        chk = 1
        for v in vals:
            b = chk >> 25
            chk = ((chk & 0x1FFFFFF) << 5) ^ v
            for i in range(5):
                chk ^= gen[i] if ((b >> i) & 1) else 0
        return chk

    def _hrp_expand(h):
        return [ord(x) >> 5 for x in h] + [0] + [ord(x) & 31 for x in h]

    if _polymod(_hrp_expand(hrp) + values) not in (1, 0x2BC830A3):
        raise ValueError("bad bech32 checksum")
    witver = values[0]
    prog_vals = values[1:-6]
    # convert 5-bit to 8-bit
    acc = 0
    bits = 0
    out = []
    for v in prog_vals:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if bits >= 5 or ((acc << (8 - bits)) & 0xFF):
        # leftover must be zero padding only
        if bits and ((acc & ((1 << bits) - 1)) != 0):
            raise ValueError("invalid padding")
    program = bytes(out)
    if witver > 16 or not (2 <= len(program) <= 40):
        raise ValueError("invalid witness program")
    return witver, program


def _scriptpubkey_from_address(addr: str) -> bytes:
    witver, prog = _bech32_decode(addr)
    if witver == 0 and len(prog) == 20:
        return bytes([0x00, 0x14]) + prog  # OP_0 PUSH20
    if witver == 0 and len(prog) == 32:
        return bytes([0x00, 0x20]) + prog
    raise ValueError(f"unsupported address type: {addr}")


def _p2wpkh_scriptcode(pubkey: bytes) -> bytes:
    return bytes([0x19, 0x76, 0xA9, 0x14]) + _hash160(pubkey) + bytes([0x88, 0xAC])


def _ser_outpoint(txid_hex: str, vout: int) -> bytes:
    return bytes.fromhex(txid_hex)[::-1] + struct.pack("<I", vout)


def _estimate_vbytes(n_in: int, n_out: int) -> int:
    # segwit v0 P2WPKH approximate
    return 10 + 68 * n_in + 31 * n_out


def _build_signed_tx(
    selected: list[dict],
    dest: str,
    amount_sats: int,
    change_addr: str,
    change_sats: int,
) -> str:
    """Build + sign a P2WPKH transaction; return hex."""
    outs = [(dest, int(amount_sats))]
    if change_sats >= DUST:
        outs.append((change_addr, int(change_sats)))

    version = struct.pack("<I", 2)
    locktime = struct.pack("<I", 0)
    sequence = struct.pack("<I", 0xFFFFFFFD)

    # prevouts / sequences for BIP143
    prevouts = b"".join(_ser_outpoint(u["txid"], int(u["vout"])) for u in selected)
    hash_prevouts = _hash256(prevouts)
    hash_sequence = _hash256(sequence * len(selected))

    out_bytes = b""
    for addr, val in outs:
        spk = _scriptpubkey_from_address(addr)
        out_bytes += struct.pack("<Q", int(val)) + _varint(len(spk)) + spk
    hash_outputs = _hash256(out_bytes)

    witnesses = []
    signed_inputs = b""
    for u in selected:
        priv = u["priv"]
        pub = u["pub"]
        if len(pub) != 33:
            pub = CCPrivateKey(priv).public_key.format(compressed=True)
        amount = int(u["amount"])
        scriptcode = _p2wpkh_scriptcode(pub)
        preimage = (
            version
            + hash_prevouts
            + hash_sequence
            + _ser_outpoint(u["txid"], int(u["vout"]))
            + scriptcode
            + struct.pack("<Q", amount)
            + sequence
            + hash_outputs
            + locktime
            + struct.pack("<I", 0x01)  # SIGHASH_ALL
        )
        digest = _hash256(preimage)
        sk = CCPrivateKey(priv)
        sig = sk.sign(digest, hasher=None)  # DER
        if not sig.endswith(b"\x01"):
            sig = sig + b"\x01"
        witnesses.append(_varint(2) + _varint(len(sig)) + sig + _varint(len(pub)) + pub)
        signed_inputs += _ser_outpoint(u["txid"], int(u["vout"])) + b"\x00" + sequence

    tx = (
        version
        + b"\x00\x01"  # marker + flag (segwit)
        + _varint(len(selected))
        + signed_inputs
        + _varint(len(outs))
        + out_bytes
        + b"".join(witnesses)
        + locktime
    )
    return tx.hex()


async def get_balance(address: str) -> float | None:
    try:
        def _bal():
            data = _get(f"/address/{address}")
            chain = data.get("chain_stats") or {}
            mem = data.get("mempool_stats") or {}
            funded = int(chain.get("funded_txo_sum") or 0) + int(mem.get("funded_txo_sum") or 0)
            spent = int(chain.get("spent_txo_sum") or 0) + int(mem.get("spent_txo_sum") or 0)
            return max(funded - spent, 0) / UNITS

        return await asyncio.to_thread(_bal)
    except Exception as exc:
        print(f"[wallet.ltc] balance failed {address}: {exc}")
        return None


async def get_receipts(address: str) -> list[dict] | None:
    """Recent funding outs — used by deposit poller; fall back to balance-delta if None."""
    try:
        def _rx():
            txs = _get(f"/address/{address}/txs")
            if not isinstance(txs, list):
                return None
            out = []
            seen = set()
            for tx in txs[:30]:
                txid = str(tx.get("txid") or "")
                for i, vout in enumerate(tx.get("vout") or []):
                    spk = (vout.get("scriptpubkey_address") or "").strip()
                    if spk.lower() != address.lower():
                        continue
                    rid = f"{txid}:{i}"
                    if rid in seen:
                        continue
                    seen.add(rid)
                    amt = int(vout.get("value") or 0)
                    if amt > 0:
                        out.append({"id": rid, "type": "receipt", "amount": amt})
            return out

        return await asyncio.to_thread(_rx)
    except Exception as exc:
        print(f"[wallet.ltc] receipts failed {address}: {exc}")
        return None


def _utxos_sync(address: str) -> list[dict]:
    data = _get(f"/address/{address}/utxo")
    out = []
    for ref in data or []:
        out.append(
            {
                "txid": ref["txid"],
                "vout": int(ref["vout"]),
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


def _broadcast_selected(selected: list[dict], dest: str, amount_sats: int, change_addr: str):
    n_in = len(selected)
    total_in = sum(int(u["amount"]) for u in selected)
    # try with/without change output for fee
    for with_change in (True, False):
        n_out = 2 if with_change else 1
        fee = max(FEE_RATE_SAT_VB * _estimate_vbytes(n_in, n_out), 500)
        change = total_in - int(amount_sats) - fee
        if with_change and change < DUST:
            continue
        if not with_change:
            # burn remainder as fee
            if total_in < amount_sats + fee:
                continue
            change = 0
        if change < 0:
            continue
        raw = _build_signed_tx(
            selected,
            dest,
            amount_sats,
            change_addr,
            change if with_change else 0,
        )
        txid = _post_tx(raw)
        return {"ok": True, "txid": txid, "fee_sats": fee}
    raise RuntimeError("insufficient LTC for amount+fee")


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
            _a, _wif, priv, pub = derive_ltc(idx, change=is_change)
        except Exception as exc:
            return {"error": f"derive failed: {exc}"}
        for u in await list_utxos(addr):
            u["priv"] = priv
            u["pub"] = pub
            all_utxos.append(u)

    if not all_utxos:
        return {"error": "no UTXOs available"}

    fee_pad = FEE_RATE_SAT_VB * _estimate_vbytes(min(len(all_utxos), 5), 2)
    selected = select_largest_first(all_utxos, amount_sats + fee_pad)
    if selected is None:
        # retry with larger pad / more inputs
        selected = select_largest_first(all_utxos, amount_sats + fee_pad * 2)
    if selected is None:
        return {"error": "insufficient LTC balance"}

    change_addr, _, _, _ = derive_ltc(CHANGE_INDEX, change=True)
    from wallet.registry import ensure_change_registered

    await ensure_change_registered("ltc")

    try:
        return await asyncio.to_thread(_broadcast_selected, selected, dest, amount_sats, change_addr)
    except Exception as exc:
        print(f"[wallet.ltc] transfer failed: {exc}")
        return {"error": str(exc)}
