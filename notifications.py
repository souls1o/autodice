import config
from bets import format_bet_display, get_bet_info
from services import get_house_balance_usd

GAMEMODE_LABELS = {
    "7s": "I Win ALL 7s",
    "7s_ties": "I Win ALL 7's & Ties",
    "ties": "I Win Ties",
    "fair": "Fair",
}


async def _send_admin_dm(bot, content):
    try:
        admin = bot.get_user(config.ADMIN_USER_ID)
        if admin is None:
            admin = await bot.fetch_user(config.ADMIN_USER_ID)
        await admin.send(content)
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
    his_bet_usd, my_bet_usd, coin = get_bet_info(form)
    gamemode = GAMEMODE_LABELS.get(
        form.get("responses", {}).get("gamemode", "fair"),
        form.get("responses", {}).get("gamemode", "fair"),
    )
    coin_label = coin.upper()
    profit_on_win = my_bet_usd - his_bet_usd
    await _send_admin_dm(
        bot,
        f"**🎮 Game Started**\n"
        f"**Channel:** {_channel_label(channel)}\n"
        f"**Gamemode:** {gamemode}\n"
        f"**Your bet:** `${format_bet_display(my_bet_usd)}` {coin_label}\n"
        f"**Their bet:** `${format_bet_display(his_bet_usd)}` {coin_label}\n"
        f"**Profit on win:** `${format_bet_display(profit_on_win)}`",
    )


async def notify_admin_game_result(bot, channel, form, self_won):
    outcome = "Win" if self_won else "Loss"
    emoji = "✅" if self_won else "❌"
    house_balance = await get_house_balance_usd()
    ticket_balance = form.get("winnings_usd", 0.0)
    new_balance = house_balance + ticket_balance
    await _send_admin_dm(
        bot,
        f"**{emoji} Game {outcome}**\n"
        f"**Channel:** {_channel_label(channel)}\n"
        f"**New balance:** `${new_balance:,.2f}`",
    )
