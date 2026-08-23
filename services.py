import requests
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
import certifi
import config
from bets import UNITS, get_bet_info, get_price

mongo_client = AsyncIOMotorClient(
    config.MONGO_URI,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=5_000,
    connectTimeoutMS=5_000,
    socketTimeoutMS=10_000,
)
db = mongo_client[config.DB_NAME]
stats_collection = db.stats
tickets_collection = db.tickets
history_collection = db.game_history

HISTORY_PAGE_SIZE = 5

HOUSE_COINS = ("btc", "eth", "ltc")
_EMPTY_PERIOD = {"wagered": 0.0, "profit": 0.0, "games": 0, "unique_users": []}
# Fixed Pacific Standard Time (UTC-8), matching existing daily keys.
_PST_OFFSET = timedelta(hours=8)


def _stats_now_pst():
    return datetime.utcnow() - _PST_OFFSET


def _stats_date_key(dt=None):
    """PST calendar date key used for daily buckets (YYYY-MM-DD)."""
    if dt is None:
        return _stats_now_pst().strftime("%Y-%m-%d")
    if hasattr(dt, "hour"):
        # Treat naive datetimes as UTC, same as track_stats.
        return (dt - _PST_OFFSET).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def _stats_today():
    return _stats_now_pst().date()


def _week_start_sunday(today=None):
    """Sunday of the current PST week (week starts Sunday 12:00 AM PST)."""
    today = today or _stats_today()
    # weekday(): Mon=0 ... Sun=6 → days since Sunday
    days_since_sunday = (today.weekday() + 1) % 7
    return today - timedelta(days=days_since_sunday)


def _month_start(today=None):
    """1st of the current PST month."""
    today = today or _stats_today()
    return today.replace(day=1)


def _merge_unique_users(*groups):
    seen = []
    for group in groups:
        for user_id in group or []:
            uid = str(user_id)
            if uid not in seen:
                seen.append(uid)
    return seen


def _sum_daily_range(daily, start_date, end_date):
    totals = dict(_EMPTY_PERIOD)
    totals["unique_users"] = []
    day = start_date
    while day <= end_date:
        entry = daily.get(day.strftime("%Y-%m-%d"), _EMPTY_PERIOD)
        totals["wagered"] += entry.get("wagered", 0)
        totals["profit"] += entry.get("profit", 0)
        totals["games"] += entry.get("games", 0)
        totals["unique_users"] = _merge_unique_users(
            totals["unique_users"], entry.get("unique_users")
        )
        day += timedelta(days=1)
    return totals


def _period_totals(stats, period):
    daily = stats.get("daily") or {}
    today = _stats_today()
    if period == "daily":
        # Calendar day starting 12:00 AM PST.
        entry = dict(daily.get(today.strftime("%Y-%m-%d"), _EMPTY_PERIOD))
        entry["unique_users"] = list(entry.get("unique_users") or [])
        return entry
    if period == "weekly":
        # Calendar week starting Sunday 12:00 AM PST.
        return _sum_daily_range(daily, _week_start_sunday(today), today)
    if period == "monthly":
        # Calendar month starting the 1st at 12:00 AM PST.
        return _sum_daily_range(daily, _month_start(today), today)
    all_time = stats.get("all_time") or {}
    unique = all_time.get("unique_users")
    if not unique:
        # Legacy: fall back to top-level unique_users list.
        unique = stats.get("unique_users") or []
    return {
        "wagered": all_time.get("wagered", 0),
        "profit": all_time.get("profit", 0),
        "games": all_time.get("games", 0),
        "unique_users": list(unique),
    }


def _format_money(value):
    return f"${float(value):,.2f}"


def _format_period(label, totals):
    unique_count = len(totals.get("unique_users") or [])
    return (
        f"**{label}** — Wagered {_format_money(totals['wagered'])} | "
        f"Profit {_format_money(totals['profit'])} | Games {int(totals['games'])} | "
        f"Unique {unique_count}"
    )


def _top_gamemode(stats):
    most = stats.get("most_played_gamemode") or stats.get("most_played_game") or {}
    if not isinstance(most, dict) or not most:
        return "None"
    key = max(most, key=most.get)
    from notifications import GAMEMODE_LABELS
    return GAMEMODE_LABELS.get(key, key.replace("_", " ").title())


def _format_day_label(date_key):
    """Format YYYY-MM-DD for display."""
    try:
        return datetime.strptime(date_key, "%Y-%m-%d").strftime("%b %d, %Y")
    except (TypeError, ValueError):
        return str(date_key)


def _best_and_worst_days(stats):
    """
    Return (best_key, best_profit, worst_key, worst_profit) from daily buckets.
    Ignores days with no games / missing profit.
    """
    daily = stats.get("daily") or {}
    best_key = worst_key = None
    best_profit = None
    worst_profit = None
    for key, entry in daily.items():
        if not isinstance(entry, dict):
            continue
        if int(entry.get("games", 0) or 0) <= 0:
            continue
        profit = round(float(entry.get("profit", 0) or 0), 2)
        if best_profit is None or profit > best_profit:
            best_profit = profit
            best_key = key
        if worst_profit is None or profit < worst_profit:
            worst_profit = profit
            worst_key = key
    return best_key, best_profit, worst_key, worst_profit


def _format_extreme_day(label, date_key, profit):
    if date_key is None or profit is None:
        return f"**{label}:** _None yet_"
    return f"**{label}:** {_format_day_label(date_key)} — {_format_money(profit)}"


def period_date_keys(period):
    """PST calendar date keys (YYYY-MM-DD) for daily/weekly/monthly buckets."""
    today = _stats_today()
    if period in ("daily", "today", "t"):
        return {today.strftime("%Y-%m-%d")}
    if period in ("weekly", "week", "w"):
        start = _week_start_sunday(today)
        keys = set()
        day = start
        while day <= today:
            keys.add(day.strftime("%Y-%m-%d"))
            day += timedelta(days=1)
        return keys
    if period in ("monthly", "month", "m"):
        start = _month_start(today)
        keys = set()
        day = start
        while day <= today:
            keys.add(day.strftime("%Y-%m-%d"))
            day += timedelta(days=1)
        return keys
    return None


def _gamemode_stats_key(form):
    responses = form.get("responses", {}) or {}
    gamemode = responses.get("gamemode", "fair")
    if gamemode == "lead_10":
        gamemode = "lead"
    game = responses.get("game", "dice")
    if game == "coinflip" and gamemode == "fair":
        return "cf_fair"
    if game == "coinflip" and gamemode == "lead":
        return "cf_lead"
    return gamemode


async def get_stats():
    stats = await stats_collection.find_one({"_id": "global"})
    if not stats:
        stats = {
            "_id": "global",
            "daily": {},
            "all_time": dict(_EMPTY_PERIOD),
            "most_played_gamemode": {},
            "unique_users": [],
        }
        await stats_collection.insert_one(stats)
    if not isinstance(stats.get("most_played_gamemode"), dict):
        stats["most_played_gamemode"] = stats.get("most_played_game") or {}
    if not isinstance(stats.get("most_played_game"), dict):
        stats["most_played_game"] = {}
    return stats


async def update_stats(data):
    await stats_collection.update_one({"_id": "global"}, {"$set": data}, upsert=True)


def _add_unique_user(period_entry, user_id):
    users = period_entry.setdefault("unique_users", [])
    uid = str(user_id)
    if uid not in users:
        users.append(uid)


async def track_stats(form, self_won):
    his_bet_usd, my_bet_usd, _coin = get_bet_info(form)
    # House/self stake only — not combined with the player's side.
    wagered = round(my_bet_usd, 2)
    profit = round(his_bet_usd if self_won else -my_bet_usd, 2)
    game = form.get("responses", {}).get("game", "dice")
    user_id = str(form["ticket_user_id"])

    stats = await get_stats()
    today = _stats_date_key()
    if today not in stats["daily"]:
        stats["daily"][today] = {
            "wagered": 0.0,
            "profit": 0.0,
            "games": 0,
            "unique_users": [],
        }
    day = stats["daily"][today]
    day.setdefault("unique_users", [])
    day["wagered"] = round(day.get("wagered", 0) + wagered, 2)
    day["profit"] = round(day.get("profit", 0) + profit, 2)
    day["games"] = day.get("games", 0) + 1
    _add_unique_user(day, user_id)

    all_time = stats.setdefault("all_time", {
        "wagered": 0.0,
        "profit": 0.0,
        "games": 0,
        "unique_users": [],
    })
    all_time.setdefault("unique_users", [])
    all_time["wagered"] = round(all_time.get("wagered", 0) + wagered, 2)
    all_time["profit"] = round(all_time.get("profit", 0) + profit, 2)
    all_time["games"] = all_time.get("games", 0) + 1
    _add_unique_user(all_time, user_id)

    # Keep legacy top-level unique_users in sync for older readers.
    unique = stats.setdefault("unique_users", [])
    if user_id not in unique:
        unique.append(user_id)

    most = stats.setdefault("most_played_gamemode", {})
    gm_key = _gamemode_stats_key(form)
    most[gm_key] = most.get(gm_key, 0) + 1

    await update_stats(stats)
    await track_ticket_game(form, self_won)


_HISTORY_GAMEMODE_LABELS = {
    "7s": "I Win ALL 7s",
    "7s_ties": "I Win ALL 7's & Ties",
    "ties": "I Win Ties",
    "fair": "Fair",
    "plus1": "I Get +1 on Rolls",
    "lead": "1-0 Lead",
    "lead_10": "1-0 Lead FT2",
    "cf_fair": "CF Fair",
    "cf_lead": "CF 1-0 Lead",
}


def _history_gamemode_label(form):
    responses = form.get("responses", {}) or {}
    gm_key = _gamemode_stats_key(form)
    label = _HISTORY_GAMEMODE_LABELS.get(gm_key, gm_key)
    first_to = responses.get("first_to")
    if first_to:
        label = f"{label} {str(first_to).upper()}"
    return label


async def record_game_history(form, self_won):
    """Persist one finished game for player !history lookup."""
    from bets import is_rakeback_bet

    user_id = form.get("ticket_user_id")
    if not user_id:
        return

    his_bet_usd, my_bet_usd, coin = get_bet_info(form)
    rakeback = is_rakeback_bet(form)
    if rakeback:
        player_profit = 0.0 if self_won else round(my_bet_usd, 2)
        wagered = 0.0
    else:
        player_profit = round(-his_bet_usd if self_won else my_bet_usd, 2)
        wagered = round(his_bet_usd, 2)

    responses = form.get("responses", {}) or {}
    doc = {
        "user_id": int(user_id),
        "channel_id": int(form["ticket_channel_id"]) if form.get("ticket_channel_id") else None,
        "game": responses.get("game", "dice"),
        "gamemode": _gamemode_stats_key(form),
        "gamemode_label": _history_gamemode_label(form),
        "first_to": responses.get("first_to"),
        "player_bet_usd": round(his_bet_usd, 2),
        "house_bet_usd": round(my_bet_usd, 2),
        "wagered_usd": wagered,
        "player_profit_usd": player_profit,
        "player_won": not self_won,
        "rakeback": rakeback,
        "coin": coin or "ltc",
        "created_at": datetime.utcnow(),
    }
    await history_collection.insert_one(doc)


async def build_history_text(discord_id, page=1):
    """Paginated game history for a player. Returns message text."""
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    page = max(1, page)

    user_id = int(discord_id)
    total = await history_collection.count_documents({"user_id": user_id})
    if total == 0:
        return "**📜 Game History**\n_No games recorded yet._"

    total_pages = max(1, (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * HISTORY_PAGE_SIZE
    cursor = (
        history_collection.find({"user_id": user_id})
        .sort("created_at", -1)
        .skip(skip)
        .limit(HISTORY_PAGE_SIZE)
    )
    games = await cursor.to_list(HISTORY_PAGE_SIZE)

    lines = [
        f"**📜 Game History** — Page `{page}/{total_pages}` ({total} game{'s' if total != 1 else ''})",
        "",
    ]
    start_n = skip + 1
    for i, game in enumerate(games):
        won = bool(game.get("player_won"))
        emoji = "✅" if won else "❌"
        result = "Win" if won else "Loss"
        label = game.get("gamemode_label") or game.get("gamemode") or "—"
        profit = float(game.get("player_profit_usd", 0) or 0)
        wagered = float(game.get("wagered_usd", 0) or 0)
        if wagered <= 0 and game.get("rakeback"):
            stake = float(game.get("house_bet_usd", 0) or 0)
            stake_note = f"RB `${stake:,.2f}`"
        else:
            stake_note = f"`${wagered:,.2f}`"
        created = game.get("created_at")
        if created:
            date_str = (created - _PST_OFFSET).strftime("%m/%d %H:%M")
        else:
            date_str = "—"
        profit_str = f"+${profit:,.2f}" if profit > 0 else f"-${abs(profit):,.2f}" if profit < 0 else "$0.00"
        lines.append(
            f"`{start_n + i}.` {emoji} **{result}** · {label} · {stake_note} → `{profit_str}` · {date_str}"
        )

    lines.append("")
    if total_pages > 1:
        if page < total_pages:
            lines.append(f"Next: `!history {page + 1}`")
        if page > 1:
            lines.append(f"Prev: `!history {page - 1}`")
    else:
        lines.append("_End of history._")
    return "\n".join(lines)


async def track_ticket_game(form, self_won):
    """Accumulate per-ticket game totals for admin !ticket lookup."""
    from bets import is_rakeback_bet
    from datetime import datetime

    channel_id = form.get("ticket_channel_id")
    if not channel_id:
        return

    his_bet_usd, my_bet_usd, _coin = get_bet_info(form)
    player_wagered = 0.0 if is_rakeback_bet(form) else round(his_bet_usd, 2)
    bot_wagered = round(my_bet_usd, 2)
    profit = round(his_bet_usd if self_won else -my_bet_usd, 2)
    user_id = form.get("ticket_user_id")

    # In-memory session mirror (active tickets).
    try:
        from state import get_ticket_session
        session = get_ticket_session(channel_id)
        session["games_played"] = int(session.get("games_played", 0) or 0) + 1
        session["player_wagered_usd"] = round(
            float(session.get("player_wagered_usd", 0) or 0) + player_wagered, 2
        )
        session["bot_wagered_usd"] = round(
            float(session.get("bot_wagered_usd", 0) or 0) + bot_wagered, 2
        )
        session["ticket_profit_usd"] = round(
            float(session.get("ticket_profit_usd", 0) or 0) + profit, 2
        )
        if user_id:
            session["ticket_user_id"] = user_id
    except Exception as exc:
        print(f"[track_ticket_game] session update failed: {exc}")

    await tickets_collection.update_one(
        {"_id": str(channel_id)},
        {
            "$set": {
                "channel_id": int(channel_id),
                "ticket_user_id": int(user_id) if user_id else None,
                "updated_at": datetime.utcnow(),
            },
            "$setOnInsert": {"created_at": datetime.utcnow()},
            "$inc": {
                "games_played": 1,
                "player_wagered_usd": player_wagered,
                "bot_wagered_usd": bot_wagered,
                "profit_usd": profit,
            },
        },
        upsert=True,
    )


async def get_ticket_stats(ticket_id):
    """Return ticket aggregate dict or None."""
    doc = await tickets_collection.find_one({"_id": str(ticket_id)})
    if doc and int(doc.get("games_played", 0) or 0) > 0:
        return doc

    from state import ticket_sessions
    try:
        session = ticket_sessions.get(int(ticket_id))
    except (TypeError, ValueError):
        session = None
    if not session:
        return None
    games = int(session.get("games_played", 0) or 0)
    if games <= 0:
        return None
    return {
        "_id": str(ticket_id),
        "channel_id": int(ticket_id),
        "ticket_user_id": session.get("ticket_user_id"),
        "games_played": games,
        "player_wagered_usd": float(session.get("player_wagered_usd", 0) or 0),
        "bot_wagered_usd": float(session.get("bot_wagered_usd", 0) or 0),
        "profit_usd": float(session.get("ticket_profit_usd", 0) or 0),
    }


async def build_ticket_stats_text(ticket_id):
    doc = await get_ticket_stats(ticket_id)
    if not doc:
        return None
    user_id = doc.get("ticket_user_id")
    user_line = f"<@{user_id}> (`{user_id}`)" if user_id else "_unknown_"
    return "\n".join([
        f"**🎫 Ticket** `{ticket_id}`",
        f"**Player:** {user_line}",
        f"**Games played:** {int(doc.get('games_played', 0) or 0)}",
        f"**Player wagered:** {_format_money(doc.get('player_wagered_usd', 0))}",
        f"**Bot wagered:** {_format_money(doc.get('bot_wagered_usd', 0))}",
        f"**Profit:** {_format_money(doc.get('profit_usd', 0))}",
    ])


async def build_stats_text():
    stats = await get_stats()
    best_key, best_profit, worst_key, worst_profit = _best_and_worst_days(stats)
    lines = [
        "**📊 Stats**",
        "",
        _format_period("Today", _period_totals(stats, "daily")),
        _format_period("Weekly", _period_totals(stats, "weekly")),
        _format_period("Monthly", _period_totals(stats, "monthly")),
        _format_period("All Time", _period_totals(stats, "all_time")),
        "",
        _format_extreme_day("Good day", best_key, best_profit),
        _format_extreme_day("Worst day", worst_key, worst_profit),
        f"**Most played:** {_top_gamemode(stats)}",
    ]
    return "\n".join(lines)


async def send_apirone(coin, address, amount):
    try:
        resp = requests.post(
            f"https://apirone.com/api/v2/accounts/{config.APIRONE_ACCOUNT}/transfer",
            params={"transfer-key": config.APIRONE_TRANSFER_KEY},
            json={"currency": coin.lower(), "destinations": [{"address": address, "amount": amount}]},
        )
        if resp.status_code == 200:
            return resp.json()
        return {"error": resp.text}
    except Exception as e:
        return {"error": str(e)}


async def admin_withdraw_usd(coin, address, usd_amount):
    """Send USD-equivalent of coin to address via Apirone. Returns (ok, message)."""
    from bets import STABLECOINS, WITHDRAW_COINS, UNITS, get_price, normalize_coin, usd_to_smallest_unit

    coin = normalize_coin(coin)
    if coin not in WITHDRAW_COINS:
        return False, (
            "❌ Coin must be `btc`, `eth`, `ltc`, or a stable such as "
            "`usdt@eth`, `usdt@bnb`, `usdc@eth`, `usdc@bnb`."
        )
    try:
        usd = float(usd_amount)
    except (TypeError, ValueError):
        return False, "❌ Amount must be a number (USD)."
    if usd <= 0:
        return False, "❌ Amount must be greater than 0."
    address = (address or "").strip()
    if not address:
        return False, "❌ Missing destination address."

    try:
        if coin in STABLECOINS:
            price = 1.0
            smallest = int(round(usd * UNITS[coin]))
        else:
            price = get_price(coin)
            smallest = usd_to_smallest_unit(usd, coin, price)
    except Exception as exc:
        return False, f"❌ Could not price {coin}: {exc}"
    if smallest <= 0:
        return False, "❌ Amount too small to send."

    result = await send_apirone(coin, address, smallest)
    if "error" in result:
        err = result["error"]
        return False, f"❌ Transfer failed: {err if isinstance(err, str) else err}"
    price_note = "1:1 USD" if coin in STABLECOINS else f"${price:,.2f}"
    return True, (
        f"✅ Sent **${usd:,.2f}** `{coin}` to `{address}` "
        f"({smallest} units @ {price_note})."
    )


async def create_apirone_address(coin):
    try:
        resp = requests.post(
            f"https://apirone.com/api/v2/accounts/{config.APIRONE_ACCOUNT}/addresses",
            json={"currency": coin.lower()},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("address")
    except Exception:
        pass
    return None


async def get_account_balance():
    if not config.APIRONE_ACCOUNT:
        return None
    try:
        resp = requests.get(
            f"https://apirone.com/api/v2/accounts/{config.APIRONE_ACCOUNT}/balance",
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


async def get_wallets():
    data = await get_account_balance()
    if not data:
        return {}
    wallets = []
    for entry in data.get("balance", []):
        coin = entry.get("currency", "").lower()
        if coin not in HOUSE_COINS:
            continue
        total = entry.get("total", 0)
        wallets.append({
            "currency": coin,
            "balance": total / UNITS[coin],
            "balance_smallest": total,
        })
    return {"wallets": wallets}


def _coin_balance_usd(coin, total_smallest):
    crypto = total_smallest / UNITS[coin]
    return crypto * get_price(coin)


async def get_house_balance_usd():
    data = await get_account_balance()
    if not data:
        return 0.0

    balances = {
        entry.get("currency", "").lower(): entry.get("total", 0)
        for entry in data.get("balance", [])
    }

    total_usd = 0.0
    for coin in HOUSE_COINS:
        smallest = balances.get(coin, 0)
        try:
            total_usd += _coin_balance_usd(coin, smallest)
        except Exception:
            pass
    return round(total_usd, 2)


async def get_house_balance_text():
    data = await get_account_balance()
    if not data:
        return "❌ Could not fetch house balance from Apirone."
    total_usd = await get_house_balance_usd()
    return f"**🏦 House Balance**\n*Balance:* `${total_usd:,.2f}`"
