"""Per-Discord-user ledger: wagered XP, level perks, and rakeback balance."""

from datetime import datetime

from services import db

users_collection = db.users

# Level thresholds by cumulative XP ($1 wagered = 1 XP). Level 1 at $0 wagered.
LEVEL_XP = [0, 250, 1250, 3750]
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

# One-time rakeback credits granted when a level is first reached.
LEVEL_REWARDS = {
    2: 5.0,
    3: 30.0,
    4: 100.0,
}


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


async def find_user_doc(discord_id):
    """Find a user by string _id, int _id, or discord_id — never insert."""
    queries = [{"_id": _user_id(discord_id)}]
    try:
        n = int(discord_id)
        queries.extend([
            {"_id": n},
            {"discord_id": n},
            {"discord_id": str(n)},
        ])
    except (TypeError, ValueError):
        pass
    for query in queries:
        user = await users_collection.find_one(query)
        if user:
            return user
    return None


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
        "level_rewards_claimed": 1,  # rewards paid through this level (1 = none)
        "ticket_commands_sent": False,
        "created_at": now,
        "updated_at": now,
    }


def pending_level_reward_amount(level, claimed_through=1):
    """Total unclaimed level rewards for current level vs last claimed level."""
    level = min(max(int(level or 1), 1), MAX_LEVEL)
    claimed_through = min(max(int(claimed_through or 1), 1), MAX_LEVEL)
    total = 0.0
    for lvl in range(claimed_through + 1, level + 1):
        total += float(LEVEL_REWARDS.get(lvl, 0.0))
    return round(total, 2)


async def apply_pending_level_rewards(discord_id, user=None):
    """
    Credit any unclaimed level rewards into rakeback_balance.
    Backfills existing users (e.g. level 3 → $5 + $30 = $35).
    Returns (user, credited_amount).
    """
    if user is None:
        user = await find_user_doc(discord_id)
    if not user:
        return None, 0.0

    level = min(max(int(user.get("level", 1)), 1), MAX_LEVEL)
    claimed = int(user.get("level_rewards_claimed", 1))
    credit = pending_level_reward_amount(level, claimed)
    uid = user.get("_id")

    if credit <= 0:
        if "level_rewards_claimed" not in user:
            await users_collection.update_one(
                {"_id": uid},
                {"$set": {"level_rewards_claimed": claimed}},
            )
            user["level_rewards_claimed"] = claimed
        return user, 0.0

    if "level_rewards_claimed" in user:
        match = {"_id": uid, "level_rewards_claimed": claimed}
    else:
        match = {"_id": uid, "level_rewards_claimed": {"$exists": False}}

    result = await users_collection.update_one(
        match,
        {
            "$inc": {"rakeback_balance": credit},
            "$set": {
                "level_rewards_claimed": level,
                "updated_at": datetime.utcnow(),
            },
        },
    )
    if result.modified_count:
        user["rakeback_balance"] = round(float(user.get("rakeback_balance", 0)) + credit, 4)
        user["level_rewards_claimed"] = level
        return user, credit

    user = await users_collection.find_one({"_id": uid})
    return user, 0.0


async def backfill_all_level_rewards():
    """Credit pending level rewards for every existing user (new feature rollout)."""
    credited_users = 0
    credited_total = 0.0
    cursor = users_collection.find({})
    async for doc in cursor:
        uid = doc.get("discord_id", doc.get("_id"))
        try:
            _user, amount = await apply_pending_level_rewards(uid, doc)
        except Exception as exc:
            print(f"[level_rewards] backfill failed for {uid}: {exc}")
            continue
        if amount > 0:
            credited_users += 1
            credited_total = round(credited_total + amount, 2)
    return credited_users, credited_total


async def ensure_user(discord_id):
    user = await find_user_doc(discord_id)
    if user:
        user, _ = await apply_pending_level_rewards(discord_id, user)
        return user
    doc = _default_user(discord_id)
    try:
        await users_collection.insert_one(doc)
    except Exception:
        user = await find_user_doc(discord_id)
        if user:
            user, _ = await apply_pending_level_rewards(discord_id, user)
            return user
        raise
    return doc


async def get_user(discord_id):
    return await ensure_user(discord_id)


async def claim_mm_ticket_commands_notify(discord_id):
    """
    Mark an MM as having received ticket-command DMs (once ever).
    Returns True only on the first successful claim — caller should DM + ping.
    """
    await ensure_user(discord_id)
    result = await users_collection.update_one(
        {"_id": _user_id(discord_id), "ticket_commands_sent": {"$ne": True}},
        {
            "$set": {
                "ticket_commands_sent": True,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    return result.modified_count > 0


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
    await users_collection.update_one({"_id": user["_id"]}, ops)
    user.update(update)
    if rb_credit > 0:
        user["rakeback_balance"] = round(float(user.get("rakeback_balance", 0)) + rb_credit, 4)

    # Pay any newly unlocked level rewards into rakeback.
    user, _reward = await apply_pending_level_rewards(discord_id, user)
    return user


async def debit_rakeback(discord_id, amount_usd):
    """
    Remove rakeback balance. Returns (ok, available, requested).
    Looks up the existing user without creating a $0 duplicate.
    Compares rounded USD so float dust does not fail a real balance.
    """
    amount = round(float(amount_usd or 0), 2)
    if amount <= 0:
        user = await find_user_doc(discord_id)
        avail = round(float((user or {}).get("rakeback_balance", 0) or 0), 2)
        return True, avail, amount

    user = await find_user_doc(discord_id)
    if not user:
        return False, 0.0, amount

    available = round(float(user.get("rakeback_balance", 0) or 0), 2)
    if available < amount:
        return False, available, amount

    result = await users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$inc": {"rakeback_balance": -amount},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    ok = (result.modified_count or 0) > 0 or (result.matched_count or 0) > 0
    return ok, available, amount


async def credit_rakeback(discord_id, amount_usd):
    """Add to rakeback ledger (e.g. rollback a failed game start)."""
    amount = round(float(amount_usd or 0), 2)
    if amount <= 0:
        return
    user = await find_user_doc(discord_id) or await ensure_user(discord_id)
    await users_collection.update_one(
        {"_id": user["_id"]},
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
    Debits the player's stake only (e.g. $60), never house my_bet (e.g. $180).
    """
    from bets import is_rakeback_bet, player_rakeback_stake_usd

    if not is_rakeback_bet(form):
        return True, None, 0.0
    user_id = form.get("ticket_user_id")
    if not user_id:
        return False, "❌ Missing ticket user for rakeback bet.", 0.0
    stake = player_rakeback_stake_usd(form)
    if stake <= 0:
        return False, "❌ Invalid rakeback stake — game cancelled.", 0.0
    ok, available, requested = await debit_rakeback(user_id, stake)
    if not ok:
        from bets import format_bet_display
        return (
            False,
            f"❌ Insufficient rakeback balance — need `${format_bet_display(requested)}`, "
            f"have `${format_bet_display(available)}`.",
            0.0,
        )
    return True, None, stake


async def record_user_wager_on_game_start(form):
    """Record wagered/XP and credit earned rakeback for cash wagers (after confirms)."""
    from bets import get_bet_info, is_rakeback_bet

    user_id = form.get("ticket_user_id")
    if not user_id:
        return None
    # Rakeback runs do not count toward wagered/XP/level.
    if is_rakeback_bet(form):
        return await ensure_user(user_id)
    his_bet_usd, _my_bet, _coin = get_bet_info(form)
    user = await add_user_wagered(
        user_id,
        his_bet_usd,
        credit_rakeback=True,
    )
    apply_user_perks_to_form(form, user)
    return user


async def add_user_profit(discord_id, amount_usd):
    """Accumulate player profit (positive = player ahead)."""
    amount = round(float(amount_usd or 0), 2)
    if amount == 0:
        return await ensure_user(discord_id)
    user = await ensure_user(discord_id)
    await users_collection.update_one(
        {"_id": user["_id"]},
        {
            "$inc": {"profit": amount},
            "$set": {"updated_at": datetime.utcnow()},
        },
    )
    return await get_user(discord_id)


async def record_user_profit_on_game_end(form, self_won):
    """
    Player profit tracking.
    Cash games: +house stake on win, −their stake on loss.
    Rakeback games: +house stake on win only (losses do not reduce profit).
    """
    from bets import get_bet_info, is_rakeback_bet

    user_id = form.get("ticket_user_id")
    if not user_id:
        return None
    his_bet_usd, my_bet_usd, _coin = get_bet_info(form)
    if is_rakeback_bet(form):
        if self_won:
            return await ensure_user(user_id)
        delta = round(my_bet_usd, 2)
    else:
        delta = round(-his_bet_usd if self_won else my_bet_usd, 2)
    return await add_user_profit(user_id, delta)


def _fmt_money(value):
    return f"${float(value):,.2f}"


def _fmt_pct(fraction):
    return f"{float(fraction) * 100:.1f}%".replace(".0%", "%")


def next_level_wager_amount(level):
    """Cumulative wagered required to reach the next level, or None at max."""
    level = min(max(int(level or 1), 1), MAX_LEVEL)
    if level >= MAX_LEVEL:
        return None
    return float(LEVEL_XP[level])


def wagered_until_next_level(wagered, level):
    nxt = next_level_wager_amount(level)
    if nxt is None:
        return 0.0
    return max(0.0, round(float(nxt) - float(wagered or 0), 2))


async def build_profile_text(discord_id, *, create=True, for_admin_lookup=False):
    """
    Build the profile stats message.
    If create=False and the user is missing, returns None.
    """
    if create:
        user = await ensure_user(discord_id)
    else:
        user = await users_collection.find_one({"_id": _user_id(discord_id)})
        if not user:
            return None
        user, _ = await apply_pending_level_rewards(discord_id, user)

    level = int(user.get("level", 1))
    wagered = round(float(user.get("wagered", 0) or 0), 2)
    rb_pct, fair_edge = perks_for_level(level)
    # Prefer stored perks (kept in sync), fall back to level table.
    rb_pct = float(user.get("rakeback_pct", rb_pct))
    fair_edge = float(user.get("fair_edge", fair_edge))
    claimable = round(float(user.get("rakeback_balance", 0)), 2)

    nxt = next_level_wager_amount(level)
    next_reward = LEVEL_REWARDS.get(level + 1)
    if nxt is None or next_reward is None:
        wagered_note = " *(max level)*"
        level_note = " *(max level)*"
    else:
        remaining = wagered_until_next_level(wagered, level)
        wagered_note = f" *({_fmt_money(remaining)}/{_fmt_money(nxt)} to level up)*"
        level_note = f" *(+{_fmt_money(next_reward)} next level)*"

    title = "**👤 Profile**"
    if for_admin_lookup:
        title = f"**👤 Profile** — <@{int(discord_id)}> (`{discord_id}`)"

    return "\n".join([
        title,
        f"Wagered: `{_fmt_money(wagered)}`{wagered_note}",
        f"Profit: `{_fmt_money(user.get('profit', 0))}`",
        f"Level: `{level}/{MAX_LEVEL}`{level_note}",
        f"Rakeback Rate: `{_fmt_pct(rb_pct)}` *(+0.5% each level)*",
        f"Fair House Edge: `{_fmt_pct(fair_edge)}` *(-1% each level)*",
        f"Rakeback: `{_fmt_money(claimable)}` *($1 minimum claim)*",
    ])


async def build_leaderboard_text():
    """Top 5 highest profit and top 5 most negative profit players."""
    projection = {"discord_id": 1, "profit": 1}
    top = await users_collection.find({}, projection).sort("profit", -1).limit(5).to_list(5)
    bottom = await users_collection.find({}, projection).sort("profit", 1).limit(5).to_list(5)

    def _line(rank, user):
        uid = user.get("discord_id") or user.get("_id")
        profit = round(float(user.get("profit", 0) or 0), 2)
        return f"`{rank}.` <@{uid}> — `{_fmt_money(profit)}`"

    lines = ["**🏆 Leaderboard**", "", "**Top Profit**"]
    if top:
        lines.extend(_line(i, u) for i, u in enumerate(top, 1))
    else:
        lines.append("_No players yet._")
    lines.extend(["", "**Most Negative**"])
    if bottom:
        lines.extend(_line(i, u) for i, u in enumerate(bottom, 1))
    else:
        lines.append("_No players yet._")
    return "\n".join(lines)


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
