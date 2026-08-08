"""Per-Discord-user ledger: wagered XP, level perks, and rakeback balance."""

from datetime import datetime

from services import db

users_collection = db.users

# Level thresholds by cumulative XP ($1 wagered = 1 XP). Level 1 at $0 wagered.
LEVEL_XP = [0, 250, 1250, 5000]
MAX_LEVEL = 4

# level -> (rakeback_pct, fair_edge)
LEVEL_PERKS = {
    1: (0.015, 0.10),
    2: (0.020, 0.09),
    3: (0.025, 0.08),
    4: (0.030, 0.07),
}

# Minimum rakeback balance that can be used as a ticket bet (`rakeback` / `rb`).
MIN_RAKEBACK_CLAIM = 1.0


def level_from_xp(xp):
    xp = float(xp or 0)
    level = 1
    for i, threshold in enumerate(LEVEL_XP):
        if xp >= threshold:
            level = i + 1
    return min(level, MAX_LEVEL)


def perks_for_level(level):
    level = min(max(int(level or 1), 1), MAX_LEVEL)
    return LEVEL_PERKS[level]


def _user_id(discord_id):
    return str(discord_id)


def _default_user(discord_id):
    rakeback_pct, fair_edge = perks_for_level(1)
    now = datetime.utcnow()
    return {
        "_id": _user_id(discord_id),
        "discord_id": int(discord_id),
        "wagered": 0.0,
        "profit": 0.0,
        "xp": 0.0,
        "level": 1,
        "rakeback_pct": rakeback_pct,
        "fair_edge": fair_edge,
        "rakeback_balance": 0.0,
        "created_at": now,
        "updated_at": now,
    }


async def ensure_user(discord_id):
    uid = _user_id(discord_id)
    user = await users_collection.find_one({"_id": uid})
    if user:
        return user
    doc = _default_user(discord_id)
    try:
        await users_collection.insert_one(doc)
    except Exception:
        user = await users_collection.find_one({"_id": uid})
        if user:
            return user
        raise
    return doc


async def get_user(discord_id):
    return await ensure_user(discord_id)


def apply_user_perks_to_form(form, user):
    if not form or not user:
        return
    default_rb, default_edge = LEVEL_PERKS[1]
    form["fair_edge"] = float(user.get("fair_edge", default_edge))
    form["rakeback_pct"] = float(user.get("rakeback_pct", default_rb))
    form["user_level"] = int(user.get("level", 1))


async def attach_user_to_form(form):
    """Ensure DB user exists and copy level perks onto the form."""
    if not form or not form.get("ticket_user_id"):
        return None
    user = await ensure_user(form["ticket_user_id"])
    apply_user_perks_to_form(form, user)
    return user


def _recompute_progress(wagered):
    xp = round(float(wagered or 0), 2)
    level = level_from_xp(xp)
    rakeback_pct, fair_edge = perks_for_level(level)
    return xp, level, rakeback_pct, fair_edge


async def add_user_wagered(discord_id, amount_usd, *, credit_rakeback=True):
    """Add wagered/XP, refresh level perks, optionally credit rakeback ledger."""
    amount = round(float(amount_usd or 0), 2)
    if amount <= 0:
        return await ensure_user(discord_id)

    user = await ensure_user(discord_id)
    new_wagered = round(float(user.get("wagered", 0)) + amount, 2)
    xp, level, rakeback_pct, fair_edge = _recompute_progress(new_wagered)

    # Credit rakeback using the rate earned at this wager (post-level update).
    rb_credit = 0.0
    if credit_rakeback:
        rb_credit = round(amount * rakeback_pct, 4)

    update = {
        "wagered": new_wagered,
        "xp": xp,
        "level": level,
        "rakeback_pct": rakeback_pct,
        "fair_edge": fair_edge,
        "updated_at": datetime.utcnow(),
    }
    inc = {}
    if rb_credit > 0:
        inc["rakeback_balance"] = rb_credit

    ops = {"$set": update}
    if inc:
        ops["$inc"] = inc
    await users_collection.update_one({"_id": _user_id(discord_id)}, ops)
    user.update(update)
    if rb_credit > 0:
        user["rakeback_balance"] = round(float(user.get("rakeback_balance", 0)) + rb_credit, 4)
    return user


async def debit_rakeback(discord_id, amount_usd):
    """Atomically remove rakeback balance. Returns True if deducted."""
    amount = round(float(amount_usd or 0), 2)
    if amount <= 0:
        return True
    await ensure_user(discord_id)
    result = await users_collection.update_one(
        {"_id": _user_id(discord_id), "rakeback_balance": {"$gte": amount}},
        {
            "$inc": {"rakeback_balance": -amount},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    return result.modified_count > 0


async def credit_rakeback(discord_id, amount_usd):
    """Add to rakeback ledger (e.g. rollback a failed game start)."""
    amount = round(float(amount_usd or 0), 2)
    if amount <= 0:
        return
    await ensure_user(discord_id)
    await users_collection.update_one(
        {"_id": _user_id(discord_id)},
        {
            "$inc": {"rakeback_balance": amount},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )


async def try_apply_rakeback_bet(response, form, user_id):
    """
    If response is rakeback/rb, set form bet fields from ledger balance.
    Returns (handled, error_message). handled=False means use normal bet parsing.
    """
    text = (response or "").strip().lower()
    if text not in ("rakeback", "rb"):
        return False, None

    from bets import format_bet_display, get_max_bet

    user = await ensure_user(user_id)
    apply_user_perks_to_form(form, user)
    bal = round(float(user.get("rakeback_balance", 0)), 2)
    if bal < MIN_RAKEBACK_CLAIM:
        return True, f"❌ No claimable rakeback (min `${format_bet_display(MIN_RAKEBACK_CLAIM)}`)."
    max_bet = get_max_bet(form)
    if bal > max_bet:
        return True, f"❌ Rakeback `${format_bet_display(bal)}` exceeds max bet `${max_bet}`."

    form["responses"]["bet"] = f"{format_bet_display(bal)} rakeback"
    form["rakeback_stake"] = bal
    form["rakeback_bet"] = True
    return True, None


async def debit_rakeback_stake_for_form(form):
    """
    Debit rakeback stake if this is a rakeback bet.
    Returns (ok, error_message, amount_debited).
    """
    from bets import get_bet_info, is_rakeback_bet

    if not is_rakeback_bet(form):
        return True, None, 0.0
    user_id = form.get("ticket_user_id")
    if not user_id:
        return False, "❌ Missing ticket user for rakeback bet.", 0.0
    his_bet_usd, _my, _coin = get_bet_info(form)
    stake = round(float(form.get("rakeback_stake") or his_bet_usd), 2)
    if not await debit_rakeback(user_id, stake):
        return False, "❌ Insufficient rakeback balance — game cancelled.", 0.0
    return True, None, stake


async def record_user_wager_on_game_start(form):
    """Record wagered/XP and credit earned rakeback for cash wagers (after confirms)."""
    from bets import get_bet_info, is_rakeback_bet

    user_id = form.get("ticket_user_id")
    if not user_id:
        return None
    his_bet_usd, _my_bet, _coin = get_bet_info(form)
    user = await add_user_wagered(
        user_id,
        his_bet_usd,
        credit_rakeback=not is_rakeback_bet(form),
    )
    apply_user_perks_to_form(form, user)
    return user


async def add_user_profit(discord_id, amount_usd):
    """Accumulate player profit (positive = player ahead)."""
    amount = round(float(amount_usd or 0), 2)
    if amount == 0:
        return await ensure_user(discord_id)
    await ensure_user(discord_id)
    await users_collection.update_one(
        {"_id": _user_id(discord_id)},
        {
            "$inc": {"profit": amount},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    return await get_user(discord_id)


async def record_user_profit_on_game_end(form, self_won):
    """Player profit: +house stake on win, −their stake on loss."""
    from bets import get_bet_info

    user_id = form.get("ticket_user_id")
    if not user_id:
        return None
    his_bet_usd, my_bet_usd, _coin = get_bet_info(form)
    delta = round(-his_bet_usd if self_won else my_bet_usd, 2)
    return await add_user_profit(user_id, delta)


def _fmt_money(value):
    return f"${float(value):,.2f}"


def _fmt_pct(fraction):
    return f"{float(fraction) * 100:.1f}%".replace(".0%", "%")


async def build_profile_text(discord_id):
    user = await ensure_user(discord_id)
    level = int(user.get("level", 1))
    rb_pct, fair_edge = perks_for_level(level)
    # Prefer stored perks (kept in sync), fall back to level table.
    rb_pct = float(user.get("rakeback_pct", rb_pct))
    fair_edge = float(user.get("fair_edge", fair_edge))
    claimable = round(float(user.get("rakeback_balance", 0)), 2)
    claim_note = ""
    if 0 < claimable < MIN_RAKEBACK_CLAIM:
        claim_note = f" _(min {_fmt_money(MIN_RAKEBACK_CLAIM)} to use in tickets)_"

    return "\n".join([
        "**👤 Profile**",
        f"**Wagered:** {_fmt_money(user.get('wagered', 0))}",
        f"**Profit:** {_fmt_money(user.get('profit', 0))}",
        f"**Level:** {level}/{MAX_LEVEL}",
        f"**Rakeback:** {_fmt_pct(rb_pct)}",
        f"**Fair edge:** {_fmt_pct(fair_edge)}",
        f"**Claimable rakeback:** {_fmt_money(claimable)}{claim_note}",
    ])


def parse_discord_user_id(raw, *, mentions=None):
    """Parse a snowflake, <@id>, or first mention into an int user id."""
    text = (raw or "").strip()
    if not text and mentions:
        return int(mentions[0].id)
    if text.startswith("<@") and text.endswith(">"):
        text = text[2:-1]
        if text.startswith("!"):
            text = text[1:]
    return int(text)


async def admin_add_wager(target_user_id, amount_usd):
    """
    Admin helper: add wagered USD, recompute level/perks, credit rakeback.
    Returns (ok, message).
    """
    try:
        amount = float(amount_usd)
    except (TypeError, ValueError):
        return False, "❌ Amount must be a number."
    if amount <= 0:
        return False, "❌ Amount must be greater than 0."

    before = await ensure_user(target_user_id)
    before_rb = round(float(before.get("rakeback_balance", 0)), 4)
    user = await add_user_wagered(target_user_id, amount, credit_rakeback=True)
    rb_gained = round(float(user.get("rakeback_balance", 0)) - before_rb, 4)

    return True, "\n".join([
        f"✅ Added **{_fmt_money(amount)}** wagered to <@{target_user_id}>",
        f"**Wagered:** {_fmt_money(user.get('wagered', 0))}",
        f"**Level:** {int(user.get('level', 1))}/{MAX_LEVEL}",
        f"**Rakeback:** {_fmt_pct(user.get('rakeback_pct', LEVEL_PERKS[1][0]))}",
        f"**Fair edge:** {_fmt_pct(user.get('fair_edge', LEVEL_PERKS[1][1]))}",
        f"**Rakeback credited:** {_fmt_money(rb_gained)}",
        f"**Claimable rakeback:** {_fmt_money(user.get('rakeback_balance', 0))}",
    ])


def build_mm_ticket_commands_dm():
    return (
        "**🎫 Ticket commands**\n"
        "`!ltc` / `!btc` / `!eth` — get a deposit address\n"
        "`!usdt-bnb` / `!usdt-eth` — USDT on BSC / ERC-20\n"
        "`!usdc-bnb` / `!usdc-eth` — USDC on BSC / ERC-20\n"
        "`!hold` — show current winnings for this ticket\n"
        "`!profile` — wagered, profit, level, rakeback & fair edge\n"
        "`!rerun` — rerun with a new bet amount\n"
        "`!restart` — restart the bet form (only before funds are sent)\n"
        "`!cancel` — cancel and payout winnings if any"
    )
