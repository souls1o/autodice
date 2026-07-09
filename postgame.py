import asyncio
import re

import config
from bets import (
    add_winnings_usd,
    add_wagered_usd,
    format_bet_display,
    get_bet_info,
    get_price,
    get_wager_usd,
    subtract_winnings_usd,
    ticket_profit_usd,
    usd_to_smallest_unit,
)
from notifications import notify_admin_game_result
from services import create_apirone_address, send_apirone, track_stats
from state import cancel_rerun_timeout, finish_form, get_form, save_session_from_form
from forms import build_confirm_text, ticket_mention

RERUN_TIMEOUT_SECONDS = 180
GAME_NUMBER_PATTERN = re.compile(r"#(\d+)")


def tip_amount(profit_usd):
    if profit_usd <= 0:
        return 0.0
    return round(profit_usd * 0.03, 2)


async def get_next_game_number(guild):
    channel = guild.get_channel(config.GAME_LOG_CHANNEL_ID)
    if channel is None:
        return 1
    async for msg in channel.history(limit=200):
        match = GAME_NUMBER_PATTERN.search(msg.content or "")
        if match:
            return int(match.group(1)) + 1
    return 1


async def post_victory_message(guild, form):
    confirmer_id = form.get("game_confirmer_user_id")
    if not confirmer_id:
        return
    channel = guild.get_channel(config.VOUCH_CHANNEL_ID)
    if channel:
        await channel.send(f"v <@{confirmer_id}>")


async def announce_game_result(channel, form, self_won, bot_user, bot=None):
    game_num = await get_next_game_number(channel.guild)
    mention = ticket_mention(channel, form)
    his_bet_usd, my_bet_usd, _coin = get_bet_info(form)
    his_bet = format_bet_display(his_bet_usd)
    my_bet = format_bet_display(my_bet_usd)

    if self_won:
        winner, loser = bot_user.mention, mention
        winner_bet, loser_bet = my_bet, his_bet
    else:
        winner, loser = mention, bot_user.mention
        winner_bet, loser_bet = his_bet, my_bet

    text = (
        f"Game #{game_num} <:dahoodcasino:1259258576015458426>\n"
        f"<:Dices:1259259866254676049>\n"
        f"{winner} overtakes {loser}\n"
        f"{winner_bet}v{loser_bet}"
    )
    await channel.send(text)


async def record_winnings(channel, form, self_won):
    his_bet_usd, my_bet_usd, coin = get_bet_info(form)
    form.setdefault("winnings_usd", 0.0)
    form.setdefault("winnings_crypto", 0.0)
    form["winnings_coin"] = coin
    if self_won:
        add_winnings_usd(form, my_bet_usd + his_bet_usd, coin)
    else:
        subtract_winnings_usd(form, my_bet_usd, coin)
    save_session_from_form(channel.id, form)


async def payout_winnings_if_any(channel, form):
    winnings_usd = form.get("winnings_usd", 0)
    winnings_crypto = form.get("winnings_crypto", 0)
    if winnings_usd > 0 and winnings_crypto > 0:
        coin = form.get("winnings_coin", "ltc")
        address = await create_apirone_address(coin)
        if address:
            tip = tip_amount(ticket_profit_usd(form))
            tip_line = f" (*YOUR TIP*: `${tip}`)" if tip > 0 else ""
            await channel.send(f"`{address}`{tip_line}")
        else:
            await channel.send(f"❌ Failed to generate {coin.upper()} address.")
    finish_form(channel, form, payout=True)


async def end_game(channel, form, self_won, bot_user, bot=None):
    form.pop("game_state", None)
    await record_winnings(channel, form, self_won)
    if bot:
        await notify_admin_game_result(bot, channel, form, self_won)
    await track_stats(form, self_won)
    await post_victory_message(channel.guild, form)
    await announce_game_result(channel, form, self_won, bot_user, bot)
    mention = ticket_mention(channel, form)
    rerun_text = f"{mention} Do you want to rerun? (yes/no)"
    await channel.send(rerun_text)
    form["waiting_for_rerun"] = True
    form["rerun_timeout_task"] = asyncio.create_task(_rerun_timeout(channel))


async def _rerun_timeout(channel):
    try:
        await asyncio.sleep(RERUN_TIMEOUT_SECONDS)
        form = get_form(channel.id)
        if not form or not form.get("waiting_for_rerun"):
            return
        form["waiting_for_rerun"] = False
        await payout_winnings_if_any(channel, form)
    except asyncio.CancelledError:
        pass


async def handle_rerun_response(message, form, bot_user, start_game_fn, bot=None):
    if not form.get("waiting_for_rerun") or message.author.id != form["ticket_user_id"]:
        return

    resp = message.content.strip().lower()
    if resp not in ("yes", "no"):
        return

    cancel_rerun_timeout(form)
    form["waiting_for_rerun"] = False

    if resp == "no":
        await payout_winnings_if_any(message.channel, form)
        return

    if form.get("winnings_usd", 0) >= get_wager_usd(form):
        wager_usd, coin = get_wager_usd(form), get_bet_info(form)[2]
        subtract_winnings_usd(form, wager_usd, coin)
        add_wagered_usd(form, wager_usd)
        save_session_from_form(message.channel.id, form)
    else:
        address = form.get("payout_address")
        if not address:
            await message.channel.send("❌ No payout address on file for rerun.")
            await payout_winnings_if_any(message.channel, form)
            return
        wager_usd, coin = get_wager_usd(form), get_bet_info(form)[2]
        amount = usd_to_smallest_unit(wager_usd, coin, get_price(coin))
        result = await send_apirone(coin, address, amount)
        if "error" in result:
            err = result["error"]
            await message.channel.send(f"❌ Rerun transfer failed: {err if isinstance(err, str) else err}")
            await payout_winnings_if_any(message.channel, form)
            return
        add_wagered_usd(form, wager_usd)
        save_session_from_form(message.channel.id, form)
        await message.channel.send(f"📤 Sent `${wager_usd}` {coin.upper()} to `{address}` for rerun")

    form["waiting_for_confirm"] = True
    form["waiting_for_adder_confirm"] = False
    form["confirm_text"] = build_confirm_text(message.channel, form, bot_user)
    await message.channel.send(form["confirm_text"])
