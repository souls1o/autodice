"""Largest-first coin selection across funded addresses / UTXOs."""

from __future__ import annotations


def select_largest_first(items: list[dict], need: int, *, amount_key="amount") -> list[dict] | None:
    """
    items: [{"amount": int, ...}, ...] in smallest units.
    Returns selected subset totaling >= need, or None.
    """
    if need <= 0:
        return []
    ordered = sorted(items, key=lambda x: int(x.get(amount_key) or 0), reverse=True)
    chosen = []
    total = 0
    for it in ordered:
        amt = int(it.get(amount_key) or 0)
        if amt <= 0:
            continue
        chosen.append(it)
        total += amt
        if total >= need:
            return chosen
    return None
