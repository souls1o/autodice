import traceback
from datetime import datetime

import config
from bets import format_bet_display
from message_queue import send_user
from services import db, get_house_balance_usd

error_logs_collection = db.error_logs

GAMEMODE_LABELS = {
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


async def _send_admin_dm(bot, content):
    try:
        admin = bot.get_user(config.ADMIN_USER_ID)
        if admin is None:
            admin = await bot.fetch_user(config.ADMIN_USER_ID)
        await send_user(admin, content)
    except Exception:
        pass


def _channel_label(channel):
    if channel is None:
        return "Unknown channel"
    guild = getattr(channel, "guild", None)
    if guild:
        return f"#{channel.name} (`{channel.id}`) — {guild.name}"
    return f"#{getattr(channel, 'name', 'unknown')} (`{channel.id}`)"


async def notify_admin_ticket_added(bot, channel):
    await _send_admin_dm(
        bot,
        f"**📃 New Ticket**\n"
        f"**Channel:** {_channel_label(channel)}",
    )


async def notify_admin_game_started(bot, channel, form):
    from bets import get_match_bets, get_match_his_display

    his_bet_usd, my_bet_usd, coin, rakeback = get_match_bets(form)
    their_display = get_match_his_display(form)
    responses = form.get("responses", {})
    gm_key = responses.get("gamemode", "fair")
    gamemode = GAMEMODE_LABELS.get(gm_key, gm_key)
    first_to = responses.get("first_to")
    if first_to and gm_key in ("lead", "lead_10", "fair"):
        gamemode = f"{gamemode} {str(first_to).upper()}"
    coin_label = (coin or "ltc").upper()
    their_line = f"**Their bet:** `${format_bet_display(their_display)}` {coin_label}"
    if rakeback:
        their_line += f" _(rakeback stake `${format_bet_display(his_bet_usd)}`)_"
    await _send_admin_dm(
        bot,
        f"**🎮 Game Started**\n"
        f"**Channel:** {_channel_label(channel)}\n"
        f"**Gamemode:** {gamemode}\n"
        f"**Your bet:** `${format_bet_display(my_bet_usd)}` {coin_label}\n"
        f"{their_line}\n",
    )


async def notify_admin_game_result(bot, channel, form, self_won):
    outcome = "Win" if self_won else "Loss"
    emoji = "✅" if self_won else "❌"
    # Use cached house bal only — never block game-end on a full multi-chain scan.
    try:
        house_balance = await get_house_balance_usd()
    except Exception:
        house_balance = 0.0
    ticket_balance = form.get("winnings_usd", 0.0)
    new_balance = house_balance + ticket_balance
    await _send_admin_dm(
        bot,
        f"**{emoji} Game {outcome}**\n"
        f"**Channel:** {_channel_label(channel)}\n"
        f"**New balance:** `${new_balance:,.2f}`",
    )


async def notify_admin_error(bot, where, error, *, channel=None, extra=None):
    """Persist + DM admin a global error report."""
    err_text = str(error) if error is not None else "Unknown error"
    tb = traceback.format_exc()
    if tb.strip() == "NoneType: None":
        tb = ""
    channel_line = _channel_label(channel) if channel is not None else "n/a"
    extra_line = str(extra).strip() if extra else ""

    doc = {
        "where": str(where),
        "error": err_text[:2000],
        "traceback": (tb or "")[:8000],
        "channel": channel_line,
        "extra": extra_line[:2000],
        "created_at": datetime.utcnow(),
    }
    try:
        await error_logs_collection.insert_one(doc)
    except Exception as exc:
        print(f"[error_log] mongo insert failed: {exc}")

    lines = [
        f"**🚨 Error — `{where}`**",
        f"**Channel:** {channel_line}",
        f"**Error:** `{err_text[:500]}`",
    ]
    if extra_line:
        lines.append(f"**Context:** {extra_line[:500]}")
    if tb:
        # Keep DM readable
        short_tb = "\n".join(tb.strip().splitlines()[-8:])
        lines.append(f"```\n{short_tb[:1500]}\n```")
    try:
        await _send_admin_dm(bot, "\n".join(lines))
    except Exception as exc:
        print(f"[error_log] admin DM failed: {exc}")
    print(f"[error] {where}: {err_text}")

