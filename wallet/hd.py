"""BIP39 HD derivation for LTC (BIP84), ETH (BIP44), SOL (BIP44)."""

from __future__ import annotations

import hashlib
import threading

from bip_utils import (
    Bip39SeedGenerator,
    Bip44,
    Bip44Changes,
    Bip44Coins,
    Bip84,
    Bip84Coins,
)

from wallet.config import require_mnemonic

# Reserved receive index 0 / change path used for LTC change & ETH merge target.
CHANGE_INDEX = 0

_seed_lock = threading.Lock()
_cached_seed: bytes | None = None
_cached_fp: str | None = None
_cached_mnemonic: str | None = None


def _seed_bytes() -> bytes:
    """PBKDF2 seed — cached; BIP39 generation is intentionally slow (~100ms+)."""
    global _cached_seed, _cached_fp, _cached_mnemonic
    m = require_mnemonic()
    with _seed_lock:
        if _cached_seed is not None and _cached_mnemonic == m:
            return _cached_seed
        _cached_seed = Bip39SeedGenerator(m).Generate()
        _cached_mnemonic = m
        _cached_fp = hashlib.sha256(_cached_seed).hexdigest()[:16]
        return _cached_seed


def ticket_index(channel_id: int) -> int:
    """Deterministic receive index from Discord channel id (never 0 — reserved)."""
    raw = int(channel_id) % 2_000_000_000
    return raw if raw > 0 else 1


def channel_id_for_index(index: int) -> int | None:
    """Not reversible uniquely; registry stores channel_id explicitly."""
    return None


def derive_ltc(index: int, *, change: bool = False):
    """Return (address, wif_or_private_hex, pubkey_bytes)."""
    bip84 = Bip84.FromSeed(_seed_bytes(), Bip84Coins.LITECOIN)
    acct = bip84.Purpose().Coin().Account(0)
    chain = acct.Change(Bip44Changes.CHAIN_EXT if not change else Bip44Changes.CHAIN_INT)
    ctx = chain.AddressIndex(int(index))
    addr = ctx.PublicKey().ToAddress()
    wif = ctx.PrivateKey().ToWif()
    priv = ctx.PrivateKey().Raw().ToBytes()
    pub = ctx.PublicKey().RawCompressed().ToBytes()
    return addr, wif, priv, pub


def derive_eth(index: int):
    """Return (checksum_address, private_key_hex)."""
    bip44 = Bip44.FromSeed(_seed_bytes(), Bip44Coins.ETHEREUM)
    ctx = (
        bip44.Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(int(index))
    )
    priv = ctx.PrivateKey().Raw().ToHex()
    addr = ctx.PublicKey().ToAddress()
    return addr, priv


def derive_sol(index: int):
    """
    Return (base58_address, secret_key_32_bytes).
    Path: m/44'/501'/{index}'/0' (common Solana wallets).
    """
    bip44 = Bip44.FromSeed(_seed_bytes(), Bip44Coins.SOLANA)
    ctx = bip44.Purpose().Coin().Account(int(index)).Change(Bip44Changes.CHAIN_EXT)
    try:
        ctx = ctx.AddressIndex(0)
    except Exception:
        pass
    priv = ctx.PrivateKey().Raw().ToBytes()
    if len(priv) > 32:
        priv = priv[:32]
    from solders.keypair import Keypair

    kp = Keypair.from_seed(priv)
    return str(kp.pubkey()), bytes(kp)


def address_for(coin: str, index: int, *, change: bool = False) -> str:
    from wallet.tokens import STABLECOINS, derive_chain

    coin = (coin or "").lower()
    if coin in STABLECOINS:
        coin = derive_chain(coin)
    if coin == "ltc":
        return derive_ltc(index, change=change)[0]
    if coin in ("eth", "bnb"):
        return derive_eth(index)[0]
    if coin == "sol":
        return derive_sol(index)[0]
    raise ValueError(f"unsupported coin: {coin}")


def fingerprint() -> str:
    """Short non-secret id of the mnemonic (for registry scoping)."""
    global _cached_fp
    _seed_bytes()
    with _seed_lock:
        return _cached_fp or ""
