import asyncio
import re

import config
from bets import (
    add_wagered_usd,
    add_winnings_usd,
    bet_validator,
    display_his_bet_usd,
    format_bet_display,
    format_matchup,
    get_bet_info,
    get_max_bet,
    get_wager_usd,
    subtract_winnings_usd,
    sync_winnings_crypto,
    usd_to_crypto_amount,
)
from notifications import notify_admin_game_result
from services import create_apirone_address, track_stats
from state import cancel_rerun_timeout, finish_form, get_form, get_ticket_session, save_session_from_form
from forms import build_confirm_text, ticket_mention
from message_queue import reply_message, send_channel

RERUN_TIMEOUT_SECONDS = 180
GAME_NUMBER_PATTERN = re.compile(r"Game\s*#(\d+)", re.IGNORECASE)
GAME_NUMBER_SCAN_LIMIT = 10
_game_number_lock = asyncio.Lock()


def _parse_game_number(content):
    if not content:
        return None
    match = GAME_NUMBER_PATTERN.search(content)
    return int(match.group(1)) if match else None


async def _read_latest_logged_game_number(guild, bot=None):
    """Most recent `Game #N` in GAME_LOG_CHANNEL (history is newest-first)."""
    channel = guild.get_channel(config.GAME_LOG_CHANNEL_ID) if guild else None
    if channel is None and bot is not None:
        try:
            channel = await bot.fetch_channel(config.GAME_LOG_CHANNEL_ID)
        except Exception:
            channel = None
    if channel is None:
        return None

    async for message in channel.history(limit=GAME_NUMBER_SCAN_LIMIT):
        game_num = _parse_game_number(message.content)
        if game_num is not None:
            return game_num
    return None


async def get_next_game_number(guild, bot=None):
    """Next id = latest Game # in the log channel + 1."""
    async with _game_number_lock:
        logged = await _read_latest_logged_game_number(guild, bot)
        return (logged or 0) + 1


async def _get_guild_channel(guild, channel_id, bot=None):
    channel = guild.get_channel(channel_id) if guild else None
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
    from bets import get_match_bets, get_match_his_display

    mention = ticket_mention(ticket_channel, form)
    _his_bet_usd, my_bet_usd, _coin, _rakeback = get_match_bets(form)
    his_display = format_bet_display(get_match_his_display(form))
    my_bet = format_bet_display(my_bet_usd)

    if self_won:
        winner, loser = bot_user.mention, mention
        winner_bet, loser_bet = my_bet, his_display
    else:
        winner, loser = mention, bot_user.mention
        winner_bet, loser_bet = his_display, my_bet

    game = form.get("responses", {}).get("game", "dice")
    game_emoji = (
        "<:Coin:1259259605255720980>"
        if game == "coinflip"
        else "<:Dices:1259259866254676049>"
    )

    guild = ticket_channel.guild
    log_channel = await _get_guild_channel(guild, config.GAME_LOG_CHANNEL_ID, bot)

    # Assign + post under one lock so the next game always sees this log first.
    async with _game_number_lock:
        logged = await _read_latest_logged_game_number(guild, bot)
        game_num = (logged or 0) + 1
        text = (
            f"Game #{game_num} <:dahoodcasino:1259258576015458426>\n"
            f"{game_emoji}\n"
            f"{winner} overtakes {loser}\n"
            f"{winner_bet}v{loser_bet}"
        )
        if log_channel is not None:
            await send_channel(log_channel, text)
        else:
            print(f"[announce] GAME_LOG_CHANNEL_ID missing/unavailable; using #{game_num}")
            await send_channel(ticket_channel, text)
            return

    # Mirror to ticket outside the lock (does not affect numbering).
    if getattr(log_channel, "id", None) != getattr(ticket_channel, "id", None):
        await send_channel(ticket_channel, text)


async def record_winnings(channel, form, self_won):
    from bets import (
        add_player_hold_usd,
        add_self_hold_usd,
        get_match_bets,
        get_player_hold_usd,
        get_self_hold_usd,
        subtract_self_hold_usd,
        sync_legacy_winnings,
    )

    if form.get("winnings_recorded"):
        print(f"[hold] skip duplicate record_winnings ticket={channel.id}")
        return
    form["winnings_recorded"] = True

    # Pop stake flags first so a later parse error cannot leave stale state.
    stake_from_hold = bool(form.pop("stake_from_hold", False))
    player_stake = float(form.pop("player_stake_from_hold", 0) or 0)
    hold_deducted = float(form.pop("hold_stake_deducted", 0) or 0)
    # Keep settled_bets through announce / stats (level-up must not rewrite amounts).
    his_bet_usd, my_bet_usd, _coin, rakeback = get_match_bets(form)
    if not form.get("settled_bets"):
        print(
            f"[hold] WARN ticket={channel.id} missing settled_bets — "
            f"using match_fair_edge={form.get('match_fair_edge')} "
            f"fair_edge={form.get('fair_edge')} bets={my_bet_usd}v{his_bet_usd}"
        )
    sync_legacy_winnings(form)
    form["winnings_coin"] = "ltc"

    before_self = get_self_hold_usd(form)
    before_player = get_player_hold_usd(form)

    if self_won:
        # Stake already left self hold at confirm when stake_from_hold; credit full pot back.
        amount = my_bet_usd if rakeback else (my_bet_usd + his_bet_usd)
        add_self_hold_usd(form, amount)
    else:
        # Player won — house stake already deducted at confirm if staked from hold.
        if not stake_from_hold:
            subtract_self_hold_usd(form, my_bet_usd)
        if rakeback:
            add_player_hold_usd(form, my_bet_usd)
        else:
            add_player_hold_usd(form, his_bet_usd + my_bet_usd)

    sync_winnings_crypto(form)
    save_session_from_form(channel.id, form)
    print(
        f"[hold] ticket={channel.id} self_won={self_won} "
        f"stake_from_hold={stake_from_hold} player_stake={player_stake} "
        f"hold_deducted={hold_deducted} "
        f"bets={my_bet_usd}v{his_bet_usd} "
        f"self {before_self:.2f}->{get_self_hold_usd(form):.2f} "
        f"player {before_player:.2f}->{get_player_hold_usd(form):.2f}"
    )


async def send_rerun_shortfall_before_confirm(channel, form):
    """Send crypto shortfall BEFORE confirmation. Hold stake is only the existing hold portion."""
    from bets import get_self_hold_usd, sync_legacy_winnings

    his_bet_usd, my_bet_usd, coin = get_bet_info(form)
    wager_usd = my_bet_usd
    sync_legacy_winnings(form)
    winnings_usd = get_self_hold_usd(form)
    from_hold = round(min(winnings_usd, wager_usd), 2)
    shortfall = round(wager_usd - from_hold, 2)

    form["pending_hold_deduct"] = from_hold
    form["pending_wager_usd"] = wager_usd
    form["rerun_shortfall_sent"] = 0.0
    from bets import freeze_match_fair_edge
    freeze_match_fair_edge(form)

    if shortfall <= 0:
        await send_channel(
            channel,
            f"♻️ **Reusing `${format_bet_display(wager_usd)}` from hold "
            f"(`{format_matchup(form)}`)**",
        )
        save_session_from_form(channel.id, form)
        return True

    address = form.get("payout_address")
    if not address:
        await send_channel(channel, "❌ No payout address on file for rerun.")
        return False

    from forms import send_usd_to_mm_and_credit_hold

    ok, err = await send_usd_to_mm_and_credit_hold(form, channel, address, shortfall, coin)
    if not ok:
        await send_channel(channel, f"❌ Rerun transfer failed: {err}")
        return False

    form["rerun_shortfall_sent"] = shortfall
    form["pending_hold_deduct"] = wager_usd
    save_session_from_form(channel.id, form)
    await send_channel(
        channel,
        f"📤 Sent `${format_bet_display(shortfall)}` {coin.upper()} to `{address}` for rerun "
        f"(`{format_matchup(form)}`)",
    )
    return True


def lock_settled_bets(form, *, my_bet_usd=None, his_bet_usd=None):
    """Freeze match stake amounts for settlement / auto-log (immune to later form edits)."""
    from bets import display_his_bet_usd, freeze_match_fair_edge, is_rakeback_bet

    edge = freeze_match_fair_edge(form)
    live_his, live_my, _coin = get_bet_info(form)
    if his_bet_usd is None:
        his_bet_usd = live_his
    if my_bet_usd is None:
        my_bet_usd = live_my
    form["settled_bets"] = {
        "his_bet_usd": round(float(his_bet_usd or 0), 2),
        "my_bet_usd": round(float(my_bet_usd or 0), 2),
        "his_display_usd": round(float(display_his_bet_usd(form) or 0), 2),
        "rakeback": bool(is_rakeback_bet(form)),
        "fair_edge": float(edge if edge is not None else form.get("fair_edge", 0.10) or 0.10),
    }
    return form["settled_bets"]


async def apply_hold_after_confirm(channel, form):
    """After confirmation: subtract from self hold only if sufficient. Never sends crypto."""
    from bets import freeze_match_fair_edge, get_self_hold_usd, subtract_self_hold_usd, sync_legacy_winnings

    freeze_match_fair_edge(form)
    wager_usd = form.pop("pending_wager_usd", None)
    if wager_usd is None:
        wager_usd = get_wager_usd(form)
    planned = float(form.pop("pending_hold_deduct", 0) or 0)
    sync_legacy_winnings(form)
    available = get_self_hold_usd(form)
    deduct = round(min(planned, available, float(wager_usd or 0)), 2)
    deducted = 0.0

    if deduct > 0:
        deducted = subtract_self_hold_usd(form, deduct)
        if deducted <= 0:
            return False

    form.pop("rerun_shortfall_sent", None)
    form["stake_from_hold"] = deducted > 0
    form["hold_stake_deducted"] = float(deducted or 0)
    # Freeze the funded house stake (not a post-level-up recalculation).
    lock_settled_bets(form, my_bet_usd=float(wager_usd or 0))
    add_wagered_usd(form, wager_usd)
    sync_winnings_crypto(form)
    save_session_from_form(channel.id, form)
    return True


async def fund_rerun_wager(channel, form):
    """Legacy name — after confirm, only apply hold. Crypto already sent before confirm."""
    return await apply_hold_after_confirm(channel, form)


async def _post_game_background(channel, form, self_won, bot_user, bot):
    async def _report(where, exc):
        print(f"[end_game] {where} failed: {exc}")
        if bot:
            try:
                from notifications import notify_admin_error
                await notify_admin_error(bot, f"end_game.{where}", exc, channel=channel)
            except Exception:
                pass

    if bot:
        try:
            await notify_admin_game_result(bot, channel, form, self_won)
        except Exception as exc:
            await _report("notify_admin_game_result", exc)

    try:
        await track_stats(form, self_won)
    except Exception as exc:
        await _report("track_stats", exc)

    try:
        from services import record_game_history
        await record_game_history(form, self_won)
    except Exception as exc:
        await _report("record_game_history", exc)

    try:
        from users import record_user_profit_on_game_end
        await record_user_profit_on_game_end(form, self_won)
    except Exception as exc:
        await _report("record_user_profit", exc)

    if self_won:
        try:
            from users import credit_mm_tip_for_game
            await credit_mm_tip_for_game(form, self_won)
        except Exception as exc:
            await _report("credit_mm_tip", exc)

    try:
        await post_victory_message(channel.guild, form, bot)
    except Exception as exc:
        await _report("post_victory_message", exc)


async def get_or_create_ticket_house_address(channel, form=None, coin="ltc"):
    """One house receive address per coin per ticket; reused for !ltc / payout / refund."""
    from state import get_form, get_ticket_session, save_session_from_form

    coin = (coin or "ltc").lower()
    if coin == "sol":
        return getattr(config, "SOL_DEPOSIT_ADDRESS", None) or None
    if coin == "eth":
        return getattr(config, "ETH_DEPOSIT_ADDRESS", None) or None

    form = form or get_form(channel.id)
    session = get_ticket_session(channel.id)
    addrs = dict(session.get("house_deposit_addresses") or {})
    if form and form.get("house_deposit_addresses"):
        addrs.update(form["house_deposit_addresses"])
    existing = addrs.get(coin)
    if existing:
        if form is not None:
            form["house_deposit_addresses"] = addrs
        session["house_deposit_addresses"] = addrs
        return existing

    address = await create_apirone_address(coin)
    if not address:
        return None
    addrs[coin] = address
    session["house_deposit_addresses"] = addrs
    if form is not None:
        form["house_deposit_addresses"] = addrs
        save_session_from_form(channel.id, form)
    return address


async def post_payout_address(channel, address, coin="ltc"):
    """Post a house receive address and track deposits → self hold deductions."""
    from services import track_ticket_deposit_address

    await send_channel(channel, f"`{address}`")
    track_ticket_deposit_address(channel.id, address, coin or "ltc")


async def payout_winnings_if_any(channel, form, *, always_post_address=False):
    from bets import get_self_hold_usd, sync_legacy_winnings

    sync_legacy_winnings(form)
    sync_winnings_crypto(form)
    if get_self_hold_usd(form) > 0 or always_post_address:
        address = await get_or_create_ticket_house_address(channel, form, "ltc")
        if address:
            await post_payout_address(channel, address)
        else:
            await send_channel(channel, "❌ Failed to generate LTC address.")
    finish_form(channel, form, payout=True)


async def end_game(channel, form, self_won, bot_user, bot=None):
    # Only settle a match once — concurrent score races must not credit both sides.
    if form.get("match_settled"):
        print(
            f"[end_game] ignored duplicate settle ticket={channel.id} "
            f"self_won={self_won} (already settled)"
        )
        return
    form["match_settled"] = True

    # Persist completed match rules for future !rerun before clearing state.
    responses = form.get("responses") or {}
    if responses:
        completed = dict(responses)
        form["last_completed_responses"] = completed
        session = get_ticket_session(channel.id)
        session["last_completed_responses"] = completed

    form.pop("game_state", None)

    try:
        await record_winnings(channel, form, self_won)
    except Exception as exc:
        print(f"[end_game] record_winnings failed: {exc}")
        if bot:
            try:
                from notifications import notify_admin_error
                await notify_admin_error(bot, "end_game.record_winnings", exc, channel=channel)
            except Exception:
                pass

    try:
        await announce_game_result(channel, form, self_won, bot_user, bot)
    except Exception as exc:
        print(f"[end_game] announce_game_result failed: {exc}")
        if bot:
            try:
                from notifications import notify_admin_error
                await notify_admin_error(bot, "end_game.announce", exc, channel=channel)
            except Exception:
                pass

    mention = ticket_mention(channel, form)
    rerun_text = f"{mention} Do you want to rerun? (yes/no)\n-# !rerun to rerun with the same settings"
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
    from state import is_game_in_progress

    if is_game_in_progress(form):
        await send_channel(channel, "❌ Cannot rerun — a game is currently in progress.")
        return False

    session = get_ticket_session(channel.id)
    completed = form.get("last_completed_responses") or session.get("last_completed_responses")
    if completed:
        form["responses"] = dict(completed)
        form["last_completed_responses"] = dict(completed)

    if not form.get("responses", {}).get("bet"):
        await send_channel(channel, "❌ No completed game to rerun.")
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
        f'**Example:** "5 {coin}", "10 litecoin", or `"rakeback"` / `"rb"` '
        f"(MIN: __$5__ | MAX: __${max_bet}__)\n"
        f"-# Same rules as last completed match.",
    )
    save_session_from_form(channel.id, form)
    return True


async def finalize_rerun(channel, form, bot_user):
    from bets import clear_match_stake_freeze
    from users import attach_user_to_form

    # New match — drop prior freeze so attach can apply the post-level-up edge.
    clear_match_stake_freeze(form)
    await attach_user_to_form(form)
    if not await send_rerun_shortfall_before_confirm(channel, form):
        await payout_winnings_if_any(channel, form)
        return False

    form["pending_rerun_fund"] = True
    form["waiting_for_confirm"] = True
    form["waiting_for_adder_confirm"] = False
    form["mm_confirm_sent"] = False
    form.pop("player_conf_pending", None)
    form.pop("player_confirmed", None)
    form["confirm_text"] = build_confirm_text(channel, form, bot_user)
    await send_channel(channel, form["confirm_text"])
    save_session_from_form(channel.id, form)
    return True


async def handle_rerun_bet_response(message, form, bot_user, bot=None):
    from users import try_apply_rakeback_bet

    if not form.get("waiting_for_rerun_bet"):
        return
    if message.author.id != form["ticket_user_id"]:
        return

    response = message.content.strip()
    if not bet_validator(response, form):
        await reply_message(message, "❌ Invalid format or out of range.")
        return

    handled, err = await try_apply_rakeback_bet(response, form, message.author.id)
    if handled:
        if err:
            await reply_message(message, err)
            return
    else:
        form.pop("rakeback_bet", None)
        form.pop("rakeback_stake", None)
        form["responses"]["bet"] = response

    form["waiting_for_rerun_bet"] = False
    save_session_from_form(message.channel.id, form)
    await finalize_rerun(message.channel, form, bot_user)


async def process_rerun(channel, form, bot_user, bot=None):
    await prompt_rerun_bet(channel, form, bot_user)


async def start_new_form_from_yes(channel, form, bot_user, bot=None):
    """'yes' after a game → brand-new form (new settings), keeping hold/session funds."""
    from bets import clear_match_stake_freeze
    from forms import ask_next_step
    from state import active_forms, get_ticket_session, new_form_dict

    cancel_rerun_timeout(form)
    form["waiting_for_rerun"] = False
    form.pop("pending_rerun_fund", None)
    form.pop("pending_hold_deduct", None)
    form.pop("pending_wager_usd", None)
    clear_match_stake_freeze(form)
    save_session_from_form(channel.id, form)

    ticket_user_id = form["ticket_user_id"]
    payout_address = form.get("payout_address")
    funds_recipient_id = form.get("funds_recipient_id")

    active_forms.pop(channel.id, None)
    session = get_ticket_session(channel.id)
    session.pop("require_bot_ping", None)
    session.pop("settled_bets", None)
    session.pop("match_fair_edge", None)
    session.pop("hold_stake_deducted", None)
    session.pop("stake_from_hold", None)
    session.pop("player_stake_from_hold", None)
    new_form = new_form_dict(channel.id, ticket_user_id)
    if payout_address:
        new_form["payout_address"] = payout_address
    if funds_recipient_id:
        new_form["funds_recipient_id"] = funds_recipient_id
    active_forms[channel.id] = new_form
    from users import attach_user_to_form
    await attach_user_to_form(new_form)
    await ask_next_step(channel, bot_user)


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

    await start_new_form_from_yes(message.channel, form, bot_user, bot)
