import asyncio
import re
from bets import add_winnings_usd, get_bet_info, get_price, get_wager_usd, subtract_winnings_usd, usd_to_crypto_amount, usd_to_smallest_unit
from forms import build_confirm_text, ticket_mention
from services import create_apirone_address, send_apirone
from state import cancel_rerun_timeout, finish_form, get_form

RERUN_TIMEOUT_SECONDS = 300


async def record_winnings(form, self_won):
    his_bet_usd, my_bet_usd, coin = get_bet_info(form)
    form.setdefault("winnings_usd", 0.0)
    form.setdefault("winnings_crypto", 0.0)
    form["winnings_coin"] = coin
    if self_won:
        add_winnings_usd(form, my_bet_usd + his_bet_usd, coin)
    else:
        subtract_winnings_usd(form, my_bet_usd, coin)


async def payout_winnings_if_any(channel, form):
    if form.get("winnings_usd", 0) > 0 and form.get("winnings_crypto", 0) > 0:
        coin = form.get("winnings_coin", "ltc")
        address = await create_apirone_address(coin)
        if address:
            await channel.send(
                f"Pay ~`{form['winnings_crypto']:.8f}` {coin.upper()} "
                f"(≈ `${form['winnings_usd']:.2f}`) to `{address}`"
            )
        else:
            await channel.send(f"❌ Failed to generate {coin.upper()} address.")
    await finish_form(channel, form)


async def end_game(channel, form, self_won, bot_user):
    form.pop("game_state", None)
    await record_winnings(form, self_won)
    mention = ticket_mention(channel, form)
    await channel.send(f"{mention} Do you want to rerun? (yes/no)")
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


async def handle_rerun_response(message, form, bot_user, start_game_fn):
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
        await message.channel.send(f"📤 Sent `${wager_usd}` {coin.upper()} to `{address}` for rerun")

    form["waiting_for_confirm"] = True
    form["waiting_for_adder_confirm"] = False
    form["confirm_text"] = build_confirm_text(message.channel, form, bot_user)
    await message.channel.send(form["confirm_text"])
