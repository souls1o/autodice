"""Stablecoin metadata: USDT/USDC on Ethereum, BNB Smart Chain, Solana."""

from __future__ import annotations

# coin key -> chain + contract/mint + decimals
TOKENS = {
    "usdt@eth": {
        "chain": "eth",
        "contract": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "decimals": 6,
    },
    "usdc@eth": {
        "chain": "eth",
        "contract": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "decimals": 6,
    },
    "usdt@bnb": {
        "chain": "bnb",
        "contract": "0x55d398326f99059fF775485246999027B3197955",
        "decimals": 18,
    },
    "usdc@bnb": {
        "chain": "bnb",
        "contract": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "decimals": 18,
    },
    "usdt@sol": {
        "chain": "sol",
        "mint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        "decimals": 6,
    },
    "usdc@sol": {
        "chain": "sol",
        "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "decimals": 6,
    },
}

STABLECOINS = frozenset(TOKENS.keys())


def is_stable(coin: str) -> bool:
    return (coin or "").lower() in STABLECOINS


def token_info(coin: str) -> dict:
    coin = (coin or "").lower()
    info = TOKENS.get(coin)
    if not info:
        raise ValueError(f"unknown stablecoin: {coin}")
    return info


def chain_for(coin: str) -> str:
    """Native chain key used for HD derivation (eth|bnb|sol)."""
    coin = (coin or "").lower()
    if coin in ("ltc", "eth", "sol"):
        return coin
    if coin in TOKENS:
        return TOKENS[coin]["chain"]
    raise ValueError(f"unsupported coin: {coin}")


def derive_chain(coin: str) -> str:
    """HD path family: bnb uses eth keys (same EVM address)."""
    ch = chain_for(coin)
    return "eth" if ch == "bnb" else ch


def companion_coins(coin: str) -> list[str]:
    """
    Assets that share one receive address.
    !eth watches native ETH + USDT/USDC on ETH and BNB (same 0x).
    !sol watches native SOL + USDT/USDC on Solana.
    """
    coin = (coin or "").lower()
    if coin in ("eth", "usdt@eth", "usdc@eth", "usdt@bnb", "usdc@bnb"):
        return ["eth", "usdt@eth", "usdc@eth", "usdt@bnb", "usdc@bnb"]
    if coin in ("sol", "usdt@sol", "usdc@sol"):
        return ["sol", "usdt@sol", "usdc@sol"]
    return [coin]
