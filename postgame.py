import asyncio
import re

import config
from bets import (
    add_wagered_usd,
    add_winnings_usd,
    bet_validator,
    format_bet_display,
    get_bet_info,
    get_max_bet,
    get_price,
    get_wager_usd,
    subtract_winnings_usd,
    sync_winnings_crypto,
    ticket_profit_usd,
    usd_to_crypto_amount,
    usd_to_smallest_unit,
)
from notifications import notify_admin_game_result
from services import create_apirone_address, send_apirone, track_stats
from state import cancel_rerun_timeout, finish_form, get_form, save_session_from_form
from forms import build_confirm_text, ticket_mention
from message_queue import reply_message, send_channel

RERUN_TIMEOUT_SECONDS = 180
GAME_NUMBER_PATTERN = re.compile(r"Game #(\d+)", re.IGNORECASE)
GAME_NUMBER_SCAN_LIMIT = 30


def _parse_game_number(content):
    if not content:
        return None
    match = GAME_NUMBER_PATTERN.search(content)
    return int(match.group(1)) if match else None


def tip_amount(profit_usd):
    if profit_usd <= 0:
        return 0.0
    return round(profit_usd * 0.03, 2)


async def get_next_game_number(guild, bot=None):
    channel = guild.get_channel(config.GAME_LOG_CHANNEL_ID)
    if channel is None and bot is not None:
        try:
            channel = await bot.fetch_channel(config.GAME_LOG_CHANNEL_ID)
        except Exception:
            channel = None
    if channel is None:
        return 1

    before = None
    for _ in range(GAME_NUMBER_SCAN_LIMIT):
        fetched = False
        async for message in channel.history(limit=1, before=before):
            fetched = True
            before = message
            game_num = _parse_game_number(message.content)
            if game_num is not None:
                return game_num + 1
        if not fetched:
            break

    return 1


async def _get_guild_channel(guild, channel_id, bot=None):
    channel = guild.get_channel(channel_id)
    if channel is None and bot is not None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    return channel


async def post_victory_message(guild, form, bot=None):
    confirmer_id = form.get("game_confirmer_user_id")
    if not confirmer_id:
        return
    channel = await _get_guild_channel(guild, config.VOUCH_CHANNEL_ID, bot)
    if channel:
        await send_channel(channel, f"v <@{confirmer_id}>")


async def announce_game_result(ticket_channel, form, self_won, bot_user, bot=None):
    game_num = await get_next_game_number(ticket_channel.guild, bot)
    mention = ticket_mention(ticket_channel, form)
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
    await send_channel(ticket_channel, text)


async def record_winnings(channel, form, self_won):
    his_bet_usd, my_bet_usd, coin = get_bet_info(form)
    form.setdefault("winnings_usd", 0.0)
    form.setdefault("winnings_crypto", 0.0)
    form["winnings_coin"] = coin
    stake_from_hold = form.pop("stake_from_hold", False)
    if self_won:
        amount = my_bet_usd + his_bet_usd
        try:
            await asyncio.to_thread(add_winnings_usd, form, amount, coin)
        except Exception as exc:
            form["winnings_usd"] = round(form.get("winnings_usd", 0) + amount, 8)
            print(f"[record_winnings] crypto conversion failed: {exc}")
    elif not stake_from_hold:
        try:
            await asyncio.to_thread(subtract_winnings_usd, form, my_bet_usd, coin)
        except Exception as exc:
            print(f"[record_winnings] crypto conversion failed: {exc}")
    sync_winnings_crypto(form)
    save_session_from_form(channel.id, form)


async def fund_rerun_wager(channel, form):
    wager_usd, coin = get_wager_usd(form), get_bet_info(form)[2]
    winnings_usd = max(form.get("winnings_usd", 0), 0)
    from_hold = round(min(winnings_usd, wager_usd), 2)
    shortfall = round(wager_usd - from_hold, 2)
    deducted = 0.0

    if from_hold > 0:
        try:
            deducted = await asyncio.to_thread(subtract_winnings_usd, form, from_hold, coin)
        except Exception as exc:
            print(f"[fund_rerun_wager] hold deduction failed: {exc}")
            return False

    if shortfall > 0:
        address = form.get("payout_address")
        if not address:
            if deducted > 0:
                add_winnings_usd(form, deducted, coin)
                sync_winnings_crypto(form)
            await send_channel(channel, "❌ No payout address on file for rerun.")
            return False
        try:
            amount = usd_to_smallest_unit(shortfall, coin, get_price(coin))
        except Exception as exc:
            print(f"[fund_rerun_wager] price lookup failed: {exc}")
            if deducted > 0:
                add_winnings_usd(form, deducted, coin)
                sync_winnings_crypto(form)
            await send_channel(channel, "❌ Could not price rerun top-up.")
            return False
        result = await send_apirone(coin, address, amount)
        if "error" in result:
            err = result["error"]
            if deducted > 0:
                add_winnings_usd(form, deducted, coin)
                sync_winnings_crypto(form)
            await send_channel(channel, f"❌ Rerun transfer failed: {err if isinstance(err, str) else err}")
            return False
        await send_channel(
            channel,
            f"📤 Sent `${format_bet_display(shortfall)}` {coin.upper()} to `{address}` for rerun",
        )

    form["stake_from_hold"] = deducted > 0
    add_wagered_usd(form, wager_usd)
    sync_winnings_crypto(form)
    save_session_from_form(channel.id, form)
    return True


async def _post_game_background(channel, form, self_won, bot_user, bot):
    if bot:
        try:
            await notify_admin_game_result(bot, channel, form, self_won)
        except Exception as exc:
            print(f"[end_game] notify_admin_game_result failed: {exc}")

    try:
        await track_stats(form, self_won)
    except Exception as exc:
        print(f"[end_game] track_stats failed: {exc}")

    try:
        await post_victory_message(channel.guild, form, bot)
    except Exception as exc:
        print(f"[end_game] post_victory_message failed: {exc}")


async def payout_winnings_if_any(channel, form):
    winnings_usd = form.get("winnings_usd", 0)
    winnings_crypto = form.get("winnings_crypto", 0)
    if winnings_usd > 0 and winnings_crypto > 0:
        coin = form.get("winnings_coin", "ltc")
        address = await create_apirone_address(coin)
        if address:
            tip = tip_amount(ticket_profit_usd(form))
            tip_line = f" (*YOUR TIP*: `${tip}`)" if tip > 0 else ""
            await send_channel(channel, f"`{address}`{tip_line}")
        else:
            await send_channel(channel, f"❌ Failed to generate {coin.upper()} address.")
    finish_form(channel, form, payout=True)


async def end_game(channel, form, self_won, bot_user, bot=None):
    form.pop("game_state", None)

    try:
        await record_winnings(channel, form, self_won)
    except Exception as exc:
        print(f"[end_game] record_winnings failed: {exc}")

    try:
        await announce_game_result(channel, form, self_won, bot_user, bot)
    except Exception as exc:
        print(f"[end_game] announce_game_result failed: {exc}")

    mention = ticket_mention(channel, form)
    rerun_text = f"{mention} Do you want to rerun? (yes/no)"
    await send_channel(channel, rerun_text)
    form["waiting_for_rerun"] = True
    form["rerun_timeout_task"] = asyncio.create_task(_rerun_timeout(channel))
    save_session_from_form(channel.id, form)

    asyncio.create_task(_post_game_background(channel, form, self_won, bot_user, bot))


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


async def prompt_rerun_bet(channel, form, bot_user):
    if form.get("game_state"):
        await send_channel(channel, "❌ Cannot rerun — a game is currently in progress.")
        return False

    if not form.get("responses", {}).get("bet"):
        await send_channel(channel, "❌ No previous game to rerun.")
        return False

    cancel_rerun_timeout(form)
    form["waiting_for_rerun"] = False
    form["waiting_for_rerun_bet"] = True

    mention = ticket_mention(channel, form)
    max_bet = get_max_bet(form)
    _, _, coin = get_bet_info(form)
    await send_channel(
        channel,
        f"💸 {mention} **How much would you like to bet for the rerun?**\n\n"
        f'**Example:** "5 {coin}", "10 litecoin" (MIN: __$1__ | MAX: __${max_bet}__)',
    )
    save_session_from_form(channel.id, form)
    return True


async def finalize_rerun(channel, form, bot_user):
    form["pending_rerun_fund"] = True
    form["waiting_for_confirm"] = True
    form["waiting_for_adder_confirm"] = False
    form["confirm_text"] = build_confirm_text(channel, form, bot_user)
    await send_channel(channel, form["confirm_text"])
    save_session_from_form(channel.id, form)
    return True


async def handle_rerun_bet_response(message, form, bot_user, bot=None):
    if not form.get("waiting_for_rerun_bet"):
        return
    if message.author.id != form["ticket_user_id"]:
        return

    response = message.content.strip()
    if not bet_validator(response, form):
        await reply_message(message, "❌ Invalid format or out of range.")
        return

    form["responses"]["bet"] = response
    form["waiting_for_rerun_bet"] = False
    save_session_from_form(message.channel.id, form)
    await finalize_rerun(message.channel, form, bot_user)


async def process_rerun(channel, form, bot_user, bot=None):
    await prompt_rerun_bet(channel, form, bot_user)


async def handle_rerun_response(message, form, bot_user, start_game_fn, bot=None):
    if not form.get("waiting_for_rerun") or message.author.id != form["ticket_user_id"]:
        return

    resp = message.content.strip().lower()
    if resp not in ("yes", "no"):
        return

    if resp == "no":
        cancel_rerun_timeout(form)
        form["waiting_for_rerun"] = False
        await payout_winnings_if_any(message.channel, form)
        return

    await prompt_rerun_bet(message.channel, form, bot_user)
