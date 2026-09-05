import asyncio
import random
import time

import discord

import config
from bets import (
    bet_validator,
    calculate_my_bet,
    clear_player_hold,
    clear_self_hold,
    display_his_bet_usd,
    extract_crypto_address,
    format_bet_display,
    format_matchup,
    get_bet_info,
    get_max_bet,
    get_price,
    get_self_hold_usd,
    get_wager_usd,
    normalize_bet_response,
    normalize_coin,
    add_self_hold_usd,
    sync_winnings_crypto,
    usd_to_smallest_unit,
)
from services import create_apirone_address, send_apirone
from notifications import notify_admin_ticket_added
from message_queue import reply_message, send_channel, send_user
from state import (
    active_forms,
    cancel_active_form,
    cancel_rerun_timeout,
    finish_form,
    form_from_session,
    get_form,
    get_hold_data,
    get_ticket_session,
    is_game_in_progress,
    is_maintenance_mode,
    is_ticket_channel,
    new_form_dict,
    notify_maintenance,
    register_ticket_channel,
    save_session_from_form,
    ticket_channels,
)
from users import (
    attach_user_to_form,
    build_mm_ticket_commands_dm,
    claim_mm_ticket_commands_notify,
    try_apply_rakeback_bet,
)

LISTEN_ROLES = [1258727325265297408, 1258732498482106398]
# Extra users allowed to post deposit addresses / confirm (in addition to LISTEN_ROLES).
LISTEN_USER_IDS = [1505600256350355537]
VALIDATORS = {"bet_validator": bet_validator}
COIN_ADDRESS_COMMANDS = {
    "!ltc": "ltc",
    "!eth": "eth",
    "!sol": "sol",
}
TICKET_COMMANDS = frozenset({
    "!ltc", "!eth", "!sol",
    "!restart", "!hold", "!profile", "!rerun", "!cancel",
    "!clearhold", "!changebet", "!changeplayer", "!tip", "!withdraw",
    "!forceend",
})
TICKET_CMD_COOLDOWN_SECONDS = 3.0
_ticket_cmd_cooldown = {}  # (channel_id, user_id) -> monotonic timestamp

DM_GAMEMODES_TEXT = """**🎲 Dice Gamemodes**
1. **I Win ALL 7's** — FT3 → 2x | FT5 → 3x Bet
2. **I Win ALL 7's & Ties** — FT3 → 3x | FT5 → 3.5x Bet
3. **I Win Ties** — FT3 → 20% HIGHER | FT5 → 30% HIGHER Bet
4. **Fair** — 7–10% LOWER Bet (FT1 / FT3 / FT5, improves with level)
5. **I Get +1 on Rolls** — FT3 → 1.5x | FT5 → 2x Bet (normal +1 / crazy −1)
6. **1-0 Lead** — FT3 → 1.5x | FT2 → 2x Bet

**🪙 Coinflip**
1. **1-0 Lead** — FT3 → 1.5x | FT2 → 2x Bet
2. **Fair** — 7–10% LOWER Bet (FT1 / FT3 / FT5, improves with level)"""


def build_dm_gamemodes_text():
    return DM_GAMEMODES_TEXT


def build_dm_help_text(user_id, *, is_mm=False):
    lines = [
        "**📖 Commands**",
        "`!help` — show this list",
        "`!profile` [user_id] — your stats, or another user's (rakeback hidden unless yours/admin)",
        "`!ticket <channel_id>` — games / wagered / profit for a ticket",
        "`!gamemodes` — dice & coinflip gamemode info",
        "`!lb` / `!leaderboard` [t/w/m] — top & bottom profit, top wagered",
        "`!history` [page] — your recent game history",
        "`!housebal` / `!hb` — house balance in USD",
        "",
        "**🎫 Ticket commands**",
        "`!ltc` / `!eth` / `!sol` — get a deposit address",
        "`!hold` — show current winnings for this ticket",
        "`!profile` [user_id] — wagered, profit, level & perks",
        "`!rerun` — rerun last completed match (new bet amount)",
        "`!changebet <usd>` — change bet before a game starts",
        "`!changeplayer <user_id>` — transfer ticket before answering the form",
        "`!restart` — restart form to change rules (not during an active game)",
        "`!cancel` — cancel and payout winnings if any",
    ]
    if is_mm:
        lines.extend([
            "",
            "**💰 MM commands**",
            "`!tip` — view tip balance (1% of player wager on self wins)",
            "`!withdraw <usd|all> <ltc_address>` — withdraw tip balance",
            "`!clearhold 1|2` — clear self or player hold",
        ])
    if user_id == config.ADMIN_USER_ID:
        lines.extend([
            "",
            "**🔧 Admin**",
            "`!stats` — wagered, profit, games, and house balance",
            "`!add-wager <amount> [user]` — add wagered (updates level/perks/rakeback)",
            "`!withdraw <coin> <address> <usd>` — Apirone send (`btc`/`eth`/`ltc`/`usdt@eth`/…)",
            "`!forceend self|player` — force-finish stuck match & award hold",
            "`!wallet` — wallet addresses",
            "`!toggle maintenance` — pause tickets & auto-post",
            "`!setchannel <id>` — set auto-post channel",
        ])
    return "\n".join(lines)


def channel_can_send(channel):
    if not isinstance(channel, discord.TextChannel):
        return False
    me = channel.guild.me
    if me is None:
        return True
    perms = channel.permissions_for(me)
    return perms.view_channel and perms.send_messages


async def safe_channel_send(channel, content, *, form=None):
    if not channel_can_send(channel):
        print(f"[skip] no send permission in #{getattr(channel, 'name', '?')} ({channel.id})")
        if form is not None:
            finish_form(channel, form)
        return None
    try:
        return await send_channel(channel, content)
    except discord.Forbidden:
        print(f"[forbidden] cannot send in #{getattr(channel, 'name', '?')} ({channel.id})")
        if form is not None:
            finish_form(channel, form)
        return None


def message_starts_with(message, prefix):
    return (message.content or "").strip().lower().startswith(prefix.lower())


def is_roll_command(content):
    text = (content or "").strip()
    lower = text.lower()
    if not lower.startswith("-roll"):
        return False
    rest = text[5:]
    if not rest:
        return True
    return rest[0] == " "


def is_cf_command(content):
    return (content or "").strip().lower().startswith("-cf")


def member_has_listen_role(member):
    if member is None:
        return False
    if getattr(member, "id", None) in LISTEN_USER_IDS:
        return True
    roles = getattr(member, "roles", None) or []
    return any(role.id in LISTEN_ROLES for role in roles)


def member_has_funds_recipient_role(member):
    if member is None:
        return False
    if getattr(member, "id", None) in (config.FUNDS_RECIPIENT_USER_IDS or []):
        return True
    roles = getattr(member, "roles", None) or []
    return any(role.id in config.FUNDS_RECIPIENT_ROLE_IDS for role in roles)


async def _member_from_user(channel, user):
    member = channel.guild.get_member(user.id)
    if member is None:
        try:
            member = await channel.guild.fetch_member(user.id)
        except Exception:
            return None
    return member


async def _notify_mm_ticket_commands(channel, recipient_id):
    """DM deposit/hold commands to a new MM and ping them in the ticket."""
    member = channel.guild.get_member(recipient_id)
    if member is None:
        try:
            member = await channel.guild.fetch_member(recipient_id)
        except Exception:
            member = None
    if member is not None:
        try:
            await send_user(member, build_mm_ticket_commands_dm())
        except Exception as exc:
            print(f"[mm_commands] DM failed for {recipient_id}: {exc}")
    await send_channel(
        channel,
        f"<@{recipient_id}> I've sent you my ticket commands.",
    )


async def resolve_funds_recipient(channel, address_message):
    candidate = None
    if not address_message.author.bot:
        candidate = address_message.author
    else:
        async for msg in channel.history(limit=2, before=address_message):
            if (msg.content or "").strip().startswith("?"):
                candidate = msg.author
            break

    if candidate is None:
        return None

    member = await _member_from_user(channel, candidate)
    if member is None or not member_has_funds_recipient_role(member):
        return None
    return candidate.id


def is_adder_confirm(content):
    text = (content or "").strip().lower()
    return text.startswith("conf")


def message_references_bot(message, bot_user):
    content = message.content or ""
    if "gengardicer" in content.lower():
        return True
    if str(bot_user.id) in content:
        return True
    if f"<@{bot_user.id}>" in content or f"<@!{bot_user.id}>" in content:
        return True
    return any(user.id == bot_user.id for user in message.mentions)


def _overwrite_target_ids(channel):
    overwrites = getattr(channel, "overwrites", None)
    if not overwrites:
        return set()
    return {getattr(target, "id", None) for target in overwrites}


def is_channel_blacklisted(channel):
    """True if channel id or name matches CHANNEL_BLACKLIST (ints and/or name strings)."""
    if channel is None:
        return False
    if isinstance(channel, int):
        channel_id, name = channel, None
    else:
        channel_id = getattr(channel, "id", None)
        name = (getattr(channel, "name", None) or "").lower()

    for entry in config.CHANNEL_BLACKLIST:
        if isinstance(entry, int):
            if channel_id is not None and entry == channel_id:
                return True
            continue
        text = str(entry).strip()
        if not text:
            continue
        if text.isdigit() and channel_id is not None and int(text) == channel_id:
            return True
        if name and text.lower() == name:
            return True
    return False


def was_bot_added_to_channel(channel, bot_user, before=None):
    if is_channel_blacklisted(channel):
        return False
    member = channel.guild.get_member(bot_user.id)
    if member is None:
        return False
    try:
        can_view = channel.permissions_for(member).view_channel
    except Exception:
        return False
    if not can_view:
        return False

    bot_id = bot_user.id
    if bot_id in _overwrite_target_ids(channel):
        return True
    if before is None:
        return False

    try:
        if not before.permissions_for(member).view_channel:
            return True
    except Exception:
        return True

    before_ids = _overwrite_target_ids(before)
    after_ids = _overwrite_target_ids(channel)
    if bot_id in after_ids and bot_id not in before_ids:
        return True

    role_ids = {role.id for role in member.roles}
    return bool(role_ids & (after_ids - before_ids))


def should_process_channel(channel, message=None, bot_user=None):
    if is_channel_blacklisted(channel):
        return False
    if getattr(channel, "guild", None) and not config.is_allowed_guild(channel.guild):
        return False
    if is_ticket_channel(channel):
        return True
    if message is not None and bot_user is not None and message_references_bot(message, bot_user):
        return True
    return False


async def resolve_ticket_user_id(channel, bot_user, *, was_tracked=False):
    session = get_ticket_session(channel.id)
    if session.get("ticket_user_id"):
        return session["ticket_user_id"]

    ticket_user_id = None
    bot_referenced = False
    async for msg in channel.history(limit=30):
        if message_references_bot(msg, bot_user):
            bot_referenced = True
            ticket_user_id = msg.author.id
            break
    if not bot_referenced and not was_tracked:
        return None
    if not ticket_user_id:
        async for msg in channel.history(limit=30):
            if not msg.author.bot:
                ticket_user_id = msg.author.id
                break
    return ticket_user_id


async def handle_bot_added_to_channel(bot, channel):
    if not config.is_allowed_guild(channel.guild):
        return
    if is_maintenance_mode():
        await notify_maintenance(channel)
        return
    if register_ticket_channel(channel.id):
        await notify_admin_ticket_added(bot, channel)


def ticket_mention(channel, form):
    user = channel.guild.get_member(form["ticket_user_id"])
    return user.mention if user else f"<@{form['ticket_user_id']}>"


def format_text(text, mention, responses, bot_user, dynamic=None):
    dynamic = dynamic or {}
    result = text.replace("@mention", mention).replace("@gengardicer", bot_user.mention)
    for key, value in {**responses, **dynamic}.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result


def _normalize_gamemode(key):
    if key == "lead_10":
        return "lead"
    return key or "fair"


def question_applies(q, responses):
    """Whether a form question should be asked for the current responses."""
    game = responses.get("game")
    gamemode = _normalize_gamemode(responses.get("gamemode"))
    if "only_for" in q and game and game not in q["only_for"]:
        return False
    only_gm = q.get("only_for_gamemode")
    if only_gm:
        raw = responses.get("gamemode")
        allowed = set(only_gm)
        if gamemode not in allowed and raw not in allowed:
            return False
    skip_gm = q.get("skip_for_gamemode")
    if skip_gm:
        raw = responses.get("gamemode")
        skipped = set(skip_gm)
        if gamemode in skipped or raw in skipped:
            return False
    return True


def build_confirm_text(channel, form, bot_user):
    mention = ticket_mention(channel, form)
    responses = form.get("responses", {})
    game = responses.get("game", "dice")
    first_to = responses.get("first_to", "ft3")
    gamemode_key = _normalize_gamemode(responses.get("gamemode", "7s"))
    first = responses.get("first", "@gengardicer 1").replace("@mention", mention).replace("@gengardicer", bot_user.mention)
    mode = responses.get("mode", "normal")
    side = responses.get("side", "h")

    if gamemode_key == "plus1":
        if mode == "crazy":
            gamemode_text = f", {bot_user.mention} gets -1 on rolls"
        else:
            gamemode_text = f", {bot_user.mention} gets +1 on rolls"
    elif gamemode_key == "lead":
        gamemode_text = ", 1-0 lead"
    else:
        gamemode_text = {
            "7s": f", {bot_user.mention} wins ALL 7s",
            "7s_ties": f", {bot_user.mention} wins ALL 7s and ties",
            "ties": f", {bot_user.mention} wins ties",
            "fair": "",
        }.get(gamemode_key, "wins 7s")

    if game == "dice":
        # Normal mode: omit mode word entirely (e.g. "ft3 @bot 1, ...")
        mode_part = f"{mode} " if mode and mode != "normal" else ""
        return f"{first_to} {mode_part}{first}{gamemode_text}"
    side_label = "heads" if str(side).lower() in ("h", "heads") else "tails"
    if gamemode_key == "lead":
        return f"{first_to} 1-0 lead, {mention} {side_label}"
    return f"{first_to} {mention} {side_label}"


async def _fund_from_hold_or_saved_address(channel, form):
    """
    If hold covers the wager, reuse it (no address ask).
    If shortfall remains and a payout address is on file, send only the shortfall.
    Returns True if funding is fully handled (caller should skip listen_address).
    """
    his_bet_usd, my_bet_usd, coin = get_bet_info(form)
    wager_usd = my_bet_usd
    hold_usd = get_self_hold_usd(form)
    from_hold = round(min(hold_usd, wager_usd), 2)
    shortfall = round(wager_usd - from_hold, 2)
    address = form.get("payout_address")

    if wager_usd <= 0:
        return False

    if shortfall <= 0:
        form["pending_hold_deduct"] = from_hold
        form["pending_wager_usd"] = wager_usd
        form["waiting_for_address"] = False
        await send_channel(
            channel,
            f"♻️ **Reusing `${format_bet_display(wager_usd)}` from hold "
            f"(`{format_matchup(form)}`)**",
        )
        save_session_from_form(channel.id, form)
        return True

    if not address:
        return False

    try:
        amount = usd_to_smallest_unit(shortfall, coin, get_price(coin))
    except Exception as exc:
        print(f"[_fund_from_hold_or_saved_address] price lookup failed: {exc}")
        await send_channel(channel, "❌ Could not price top-up.")
        return False

    result = await send_apirone(coin, address, amount)
    if "error" in result:
        err = result["error"]
        await send_channel(
            channel,
            f"❌ Transfer failed: {err if isinstance(err, str) else err}",
        )
        return False

    # Credit top-up into hold so the full wager can be staked from hold on confirm
    add_self_hold_usd(form, shortfall)
    sync_winnings_crypto(form)
    form["pending_hold_deduct"] = wager_usd
    form["pending_wager_usd"] = wager_usd
    form["waiting_for_address"] = False
    await send_channel(
        channel,
        f"📤 Sent `${format_bet_display(shortfall)}` {coin.upper()} to `{address}` "
        f"(`{format_matchup(form)}`)",
    )
    save_session_from_form(channel.id, form)
    return True


async def start_ticket_form(channel, bot_user, bot=None):
    if is_channel_blacklisted(channel):
        return
    if get_form(channel.id):
        return

    was_tracked = channel.id in ticket_channels

    if is_maintenance_mode():
        await notify_maintenance(channel)
        return

    if not channel_can_send(channel):
        return

    ticket_user_id = await resolve_ticket_user_id(channel, bot_user, was_tracked=was_tracked)
    if not ticket_user_id:
        return

    register_ticket_channel(channel.id)
    session = get_ticket_session(channel.id)
    session.pop("require_bot_ping", None)
    form = new_form_dict(channel.id, ticket_user_id)
    active_forms[channel.id] = form
    await attach_user_to_form(form)
    await ask_next_step(channel, bot_user)


async def ask_next_step(channel, bot_user):
    form = get_form(channel.id)
    if not form:
        return

    while form["step"] < len(config.FORM_QUESTIONS):
        q = config.FORM_QUESTIONS[form["step"]]
        if question_applies(q, form.get("responses", {})):
            break
        form["step"] += 1

    q = config.FORM_QUESTIONS[form["step"]]
    mention = ticket_mention(channel, form)
    responses = form.get("responses", {})
    game = responses.get("game")
    fair_edge = float(form.get("fair_edge", 0.10))
    fair_pct_num = round(fair_edge * 100, 1)
    fair_pct = str(int(fair_pct_num)) if fair_pct_num == int(fair_pct_num) else f"{fair_pct_num:.1f}"
    dynamic = {
        "max_bet": get_max_bet(form),
        "game_emoji": "Dices" if game == "dice" else "Coin",
        "fair_pct": fair_pct,
    }
    question_text = format_text(q.get("text", ""), mention, responses, bot_user, dynamic)

    if q["type"] in ("choice", "open"):
        await safe_channel_send(channel, question_text, form=form)
        return

    if q["type"] == "listen_address":
        if await _fund_from_hold_or_saved_address(channel, form):
            form["step"] += 1
            await ask_next_step(channel, bot_user)
            return
        _, _, fund_coin = get_bet_info(form)
        dynamic.update({
            "coin": "ltc",
            "my_bet": format_bet_display(calculate_my_bet(form) or 0),
            "his_bet": format_bet_display(display_his_bet_usd(form)),
        })
        question_text = format_text(q.get("text", ""), mention, responses, bot_user, dynamic)
        form["waiting_for_address"] = True
    elif q["type"] == "listen_confirm":
        question_text = build_confirm_text(channel, form, bot_user)
        form["confirm_text"] = question_text
        form["waiting_for_confirm"] = True
        form["waiting_for_adder_confirm"] = False
        form["mm_confirm_sent"] = False
        form.pop("player_conf_pending", None)
        form.pop("player_confirmed", None)

    await safe_channel_send(channel, question_text, form=form)
    if q["type"] == "listen_confirm":
        mm_id = form.get("funds_recipient_id")
        if mm_id:
            await send_channel(channel, f"<@{mm_id}>")


async def handle_form_step(message, form, bot_user):
    if form["step"] >= len(config.FORM_QUESTIONS):
        return
    if form["ticket_user_id"] != message.author.id:
        return

    q = config.FORM_QUESTIONS[form["step"]]
    response = message.content.strip()
    upper_resp = response.upper()

    if q["type"] == "choice":
        output_value = None
        random_inputs = q["mapping"].get("random", [])
        if upper_resp in ("RANDOM", "R") or any(upper_resp == inp.upper() for inp in random_inputs):
            options = [val for val in q["mapping"] if val.lower() != "random"]
            output_value = random.choice(options) if options else None
        else:
            for val, inputs in q["mapping"].items():
                if val.lower() == "random":
                    continue
                if any(upper_resp == inp.upper() for inp in inputs):
                    output_value = val
                    break
        if output_value is None:
            return
        if q.get("short_key"):
            form["responses"][q["short_key"]] = output_value
        form["step"] += 1
        await ask_next_step(message.channel, bot_user)
        return

    if q["type"] == "open":
        validator = VALIDATORS.get(q.get("validator"))
        if validator and not validator(response, form):
            await reply_message(message, "❌ Invalid format or out of range.")
            return
        if q.get("validator") == "bet_validator":
            handled, err = await try_apply_rakeback_bet(response, form, message.author.id)
            if handled:
                if err:
                    await reply_message(message, err)
                    return
                form["step"] += 1
                await ask_next_step(message.channel, bot_user)
                return
            form.pop("rakeback_bet", None)
            form.pop("rakeback_stake", None)
        if q.get("short_key"):
            if q.get("validator") == "bet_validator" and q["short_key"] == "bet":
                form["responses"][q["short_key"]] = normalize_bet_response(response)
            else:
                form["responses"][q["short_key"]] = response
        form["step"] += 1
        await ask_next_step(message.channel, bot_user)


async def handle_ticket_command(message, bot_user, bot=None):
    content = message.content.strip().lower()
    cmd = content.split()[0] if content else ""

    if cmd == "!changebet":
        await handle_changebet_command(message, bot_user)
        return True
    if cmd == "!changeplayer":
        await handle_changeplayer_command(message, bot_user)
        return True
    if cmd == "!clearhold":
        from users import user_has_mm_role
        if not await user_has_mm_role(bot, message.author.id, member=message.author):
            await send_channel(message.channel, "❌ MM only command.")
            return True
        await handle_clearhold_command(message, bot_user)
        return True

    if content not in TICKET_COMMANDS and cmd not in TICKET_COMMANDS:
        return False

    key = (message.channel.id, message.author.id)
    now = time.monotonic()
    last = _ticket_cmd_cooldown.get(key, 0.0)
    if now - last < TICKET_CMD_COOLDOWN_SECONDS:
        return True  # swallow spam without running the command
    _ticket_cmd_cooldown[key] = now

    if content in COIN_ADDRESS_COMMANDS:
        coin = COIN_ADDRESS_COMMANDS[content]
        label = coin.upper()
        if coin == "sol":
            address = getattr(config, "SOL_DEPOSIT_ADDRESS", None) or None
        elif coin == "eth":
            address = getattr(config, "ETH_DEPOSIT_ADDRESS", None) or None
        else:
            address = await create_apirone_address(coin)
        if address:
            from postgame import post_payout_address
            await post_payout_address(message.channel, address)
        else:
            await send_channel(message.channel, f"❌ Failed to generate {label} address.")
        return True

    if content == "!restart":
        await handle_restart_command(message, bot_user, bot)
        return True

    if content == "!hold":
        await handle_hold_command(message, bot_user)
        return True

    if cmd == "!forceend":
        await handle_forceend_command(message, bot_user, bot)
        return True

    if cmd == "!profile":
        from users import resolve_profile_command
        parts = message.content.strip().split(maxsplit=1)
        raw_args = parts[1] if len(parts) > 1 else None
        try:
            text, err = await asyncio.wait_for(
                resolve_profile_command(
                    message.author.id,
                    raw_args,
                    mentions=message.mentions,
                ),
                timeout=8.0,
            )
        except Exception as exc:
            print(f"[ticket] !profile failed for {message.author.id}: {exc}")
            await send_channel(
                message.channel,
                "❌ Could not load profile (database timeout). Try again in a moment.",
            )
            return True
        if err:
            await send_channel(message.channel, err)
            return True
        await send_channel(message.channel, text)
        return True

    if cmd == "!tip":
        from users import build_tip_text, user_has_mm_role
        if not await user_has_mm_role(bot, message.author.id, member=message.author):
            await send_channel(message.channel, "❌ MM only command.")
            return True
        try:
            text = await asyncio.wait_for(
                build_tip_text(message.author.id),
                timeout=8.0,
            )
        except Exception as exc:
            print(f"[ticket] !tip failed for {message.author.id}: {exc}")
            await send_channel(message.channel, "❌ Could not load tip balance.")
            return True
        await send_channel(message.channel, text)
        return True

    if cmd == "!withdraw":
        from users import mm_withdraw_tip, user_has_mm_role
        if not await user_has_mm_role(bot, message.author.id, member=message.author):
            await send_channel(message.channel, "❌ MM only command.")
            return True
        parts = message.content.strip().split()
        if len(parts) != 3:
            await send_channel(
                message.channel,
                "Usage: `!withdraw <usd_amount|all> <ltc_address>`",
            )
            return True
        try:
            ok, text = await asyncio.wait_for(
                mm_withdraw_tip(message.author.id, parts[1], parts[2]),
                timeout=20.0,
            )
        except Exception as exc:
            print(f"[ticket] !withdraw failed for {message.author.id}: {exc}")
            await send_channel(message.channel, "❌ Withdraw failed (timeout or error).")
            return True
        await send_channel(message.channel, text)
        return True

    if content == "!rerun":
        await handle_rerun_command(message, bot_user, bot)
        return True

    if content == "!cancel":
        await handle_cancel_command(message, bot_user)
        return True

    return False


async def handle_hold_command(message, bot_user):
    channel = message.channel
    self_hold, player_hold, _coin = get_hold_data(channel.id)
    form = get_form(channel.id)
    if not form:
        session = get_ticket_session(channel.id)
        form = {"ticket_user_id": session.get("ticket_user_id")}
    mention = ticket_mention(channel, form)
    await send_channel(
        channel,
        f"**Hold for this ticket**\n"
        f"**{bot_user.mention}:** `${self_hold:.2f}`\n"
        f"**{mention}:** `${player_hold:.2f}`",
    )


async def handle_clearhold_command(message, bot_user):
    channel = message.channel
    parts = message.content.strip().split()
    if len(parts) < 2 or parts[1] not in ("1", "2"):
        await send_channel(channel, "Usage: `!clearhold 1` (self) or `!clearhold 2` (player)")
        return
    form = get_form(channel.id)
    session = get_ticket_session(channel.id)
    target = form if form else session
    if parts[1] == "1":
        if form:
            clear_self_hold(form)
            save_session_from_form(channel.id, form)
        else:
            session["self_hold_usd"] = 0.0
            session["winnings_usd"] = 0.0
            session["winnings_crypto"] = 0.0
        await send_channel(channel, f"✅ Cleared {bot_user.mention} hold.")
    else:
        if form:
            clear_player_hold(form)
            save_session_from_form(channel.id, form)
        else:
            session["player_hold_usd"] = 0.0
        player_form = form or {"ticket_user_id": session.get("ticket_user_id")}
        await send_channel(
            channel,
            f"✅ Cleared {ticket_mention(channel, player_form)} hold.",
        )


async def handle_changebet_command(message, bot_user):
    channel = message.channel
    form = get_form(channel.id)
    if not form:
        await send_channel(channel, "❌ No active ticket.")
        return
    if message.author.id != form.get("ticket_user_id"):
        return
    if form.get("game_state"):
        await send_channel(channel, "❌ Cannot change bet while a game is in progress.")
        return
    parts = message.content.strip().split()
    if len(parts) < 2:
        await send_channel(channel, "Usage: `!changebet <usd>`")
        return
    try:
        amount = float(parts[1])
    except ValueError:
        await send_channel(channel, "❌ Amount must be a number.")
        return
    normalized = normalize_bet_response(str(amount))
    if not bet_validator(normalized, form):
        await send_channel(channel, "❌ Invalid amount or out of range.")
        return
    form["responses"]["bet"] = normalized
    form.pop("rakeback_bet", None)
    form.pop("rakeback_stake", None)
    my_bet = calculate_my_bet(form) or 0
    player_bet = display_his_bet_usd(form)
    await send_channel(
        channel,
        f"`{format_bet_display(my_bet)}v{format_bet_display(player_bet)}`",
    )
    save_session_from_form(channel.id, form)


async def handle_changeplayer_command(message, bot_user):
    """Transfer ticket ownership — only current player, before any form answer."""
    from users import attach_user_to_form, parse_discord_user_id

    channel = message.channel
    form = get_form(channel.id)
    if not form:
        await send_channel(channel, "❌ No active ticket.")
        return
    if message.author.id != form.get("ticket_user_id"):
        return
    if form.get("step", 0) != 0 or form.get("responses"):
        await send_channel(
            channel,
            "❌ Can only change player before answering the first form question.",
        )
        return
    if form.get("game_state") or form.get("game_started"):
        await send_channel(channel, "❌ Cannot change player after a game has started.")
        return

    parts = message.content.strip().split(maxsplit=1)
    if len(parts) < 2:
        await send_channel(channel, "Usage: `!changeplayer <user_id|@mention>`")
        return
    try:
        new_id = parse_discord_user_id(parts[1], mentions=message.mentions)
    except (TypeError, ValueError):
        await send_channel(channel, "❌ Invalid user id / mention.")
        return
    if new_id == form.get("ticket_user_id"):
        await send_channel(channel, "❌ That user is already the ticket player.")
        return

    form["ticket_user_id"] = new_id
    session = get_ticket_session(channel.id)
    session["ticket_user_id"] = new_id
    await attach_user_to_form(form)
    save_session_from_form(channel.id, form)
    await send_channel(channel, f"✅ Ticket player set to <@{new_id}>.")
    await ask_next_step(channel, bot_user)


async def handle_rerun_command(message, bot_user, bot=None):
    from postgame import process_rerun

    channel = message.channel
    form = get_form(channel.id)
    if not form:
        form = form_from_session(channel.id)
    if is_game_in_progress(form):
        await send_channel(channel, "❌ Cannot rerun — a game is currently in progress.")
        return

    session = get_ticket_session(channel.id)
    completed = (form or {}).get("last_completed_responses") or session.get("last_completed_responses")
    if completed:
        if not form:
            form = form_from_session(channel.id) or new_form_dict(
                channel.id, session.get("ticket_user_id")
            )
        form["responses"] = dict(completed)
        form["last_completed_responses"] = dict(completed)
    elif not form or not form.get("responses", {}).get("bet"):
        await send_channel(channel, "❌ No completed game to rerun.")
        return

    active_forms[channel.id] = form
    session.pop("require_bot_ping", None)
    await process_rerun(channel, form, bot_user, bot)


async def handle_cancel_command(message, bot_user):
    channel = message.channel
    form = get_form(channel.id)
    if is_game_in_progress(form):
        await send_channel(channel, "❌ Cannot cancel — game has already started.")
        return

    if not form:
        session = get_ticket_session(channel.id)
        if not session.get("ticket_user_id") and session.get("winnings_usd", 0) <= 0:
            await send_channel(channel, "❌ No active ticket to cancel.")
            return
        form = new_form_dict(channel.id, session.get("ticket_user_id"))

    funds_sent = bool(form.get("payout_address"))

    cancel_rerun_timeout(form)
    form.pop("game_state", None)
    form.pop("pending_rerun_fund", None)
    form.pop("pending_hold_deduct", None)
    form.pop("pending_wager_usd", None)
    form["waiting_for_rerun"] = False
    form["waiting_for_rerun_bet"] = False
    form["waiting_for_confirm"] = False
    form["waiting_for_address"] = False
    form["waiting_for_adder_confirm"] = False
    form["mm_confirm_sent"] = False
    form.pop("player_conf_pending", None)
    form.pop("player_confirmed", None)

    from postgame import payout_winnings_if_any, post_payout_address

    if funds_sent:
        refund_address = await create_apirone_address("ltc")
        if refund_address:
            await post_payout_address(channel, refund_address)
        else:
            await send_channel(channel, "❌ Failed to generate LTC refund address.")

    active_forms[channel.id] = form
    await payout_winnings_if_any(channel, form)


async def handle_restart_command(message, bot_user, bot=None):
    channel = message.channel
    form = get_form(channel.id)
    if is_game_in_progress(form):
        await send_channel(channel, "❌ Cannot restart — a game is currently in progress.")
        return

    if form:
        # Keep funds/hold/address; clear in-flight confirm & pending stake flags.
        form.pop("game_state", None)
        form.pop("pending_rerun_fund", None)
        form.pop("pending_hold_deduct", None)
        form.pop("pending_wager_usd", None)
        form["waiting_for_rerun"] = False
        form["waiting_for_rerun_bet"] = False
        form["waiting_for_confirm"] = False
        form["waiting_for_address"] = False
        form["waiting_for_adder_confirm"] = False
        form["mm_confirm_sent"] = False
        form.pop("player_conf_pending", None)
        form.pop("player_confirmed", None)
        cancel_active_form(channel, form)

    register_ticket_channel(channel.id)
    await start_ticket_form(channel, bot_user, bot)
    await send_channel(channel, "♻️ Form restarted — pick new rules. Hold & deposit address kept.")


async def handle_forceend_command(message, bot_user, bot=None):
    """Self-only: force-finish a stuck match and award hold to the chosen winner."""
    channel = message.channel
    if message.author.id != bot_user.id:
        await send_channel(channel, "❌ Self only command.")
        return

    parts = message.content.strip().split()
    if len(parts) < 2 or parts[1].lower() not in ("self", "player", "1", "2"):
        await send_channel(
            channel,
            "Usage: `!forceend self` or `!forceend player`\n"
            "Awards hold as if that side won the in-progress match.",
        )
        return

    form = get_form(channel.id)
    if not is_game_in_progress(form):
        await send_channel(channel, "❌ No game in progress to force-end.")
        return

    winner = parts[1].lower()
    self_won = winner in ("self", "1")
    from postgame import end_game

    state = form.get("game_state") or {}
    score = f"{state.get('self_score', '?')}-{state.get('adder_score', '?')}"
    await send_channel(
        channel,
        f"⚠️ Force-ending match at `{score}` — "
        f"{'self' if self_won else 'player'} awarded.",
    )
    await end_game(channel, form, self_won, bot_user, bot)


async def handle_global_listeners(message, bot_user, start_game_fn, bot=None):
    form = get_form(message.channel.id)
    if not form:
        return

    if form.get("waiting_for_rerun"):
        from postgame import handle_rerun_response
        await handle_rerun_response(message, form, bot_user, start_game_fn, bot)
        if message.channel.id not in active_forms:
            return

    form = get_form(message.channel.id)
    if not form:
        return

    if form.get("waiting_for_rerun_bet"):
        from postgame import handle_rerun_bet_response
        await handle_rerun_bet_response(message, form, bot_user, bot)
        return

    form = get_form(message.channel.id)
    if not form:
        return

    if form.get("waiting_for_address") and member_has_listen_role(message.author):
        coin = "ltc"
        address = extract_crypto_address(message.content, coin)
        if address:
            recipient_id = await resolve_funds_recipient(message.channel, message)
            if not recipient_id:
                await send_channel(
                    message.channel,
                    "❌ Could not verify funds recipient — a staff member with the required role must post the address.",
                )
                return
            wager_usd = get_wager_usd(form)
            hold_usd = get_self_hold_usd(form)
            from_hold = round(min(hold_usd, wager_usd), 2)
            shortfall = round(wager_usd - from_hold, 2)

            if shortfall > 0:
                amount = usd_to_smallest_unit(shortfall, coin, get_price(coin))
                result = await send_apirone(coin, address, amount)
                if "error" in result:
                    err = result["error"]
                    await send_channel(
                        message.channel,
                        f"❌ Transfer failed: {err if isinstance(err, str) else err}",
                    )
                    return
                add_self_hold_usd(form, shortfall)
                sync_winnings_crypto(form)
                from_hold = wager_usd
                await send_channel(
                    message.channel,
                    f"📤 Sent `${format_bet_display(shortfall)}` {coin.upper()} to `{address}` "
                    f"(`{format_matchup(form)}`)",
                )

            form["waiting_for_address"] = False
            form["payout_address"] = address
            form["funds_recipient_id"] = recipient_id
            form["pending_hold_deduct"] = from_hold
            form["pending_wager_usd"] = wager_usd
            save_session_from_form(message.channel.id, form)
            # DM ticket commands only the first time this MM is ever seen.
            try:
                should_notify = await claim_mm_ticket_commands_notify(recipient_id)
            except Exception as exc:
                print(f"[mm_commands] claim failed for {recipient_id}: {exc}")
                should_notify = False
            if should_notify:
                await _notify_mm_ticket_commands(message.channel, recipient_id)
            form["step"] += 1
            await ask_next_step(message.channel, bot_user)

    if form.get("waiting_for_confirm") or form.get("waiting_for_adder_confirm") or form.get("mm_confirm_sent"):
        expected = form.get("confirm_text")

        # Player conf — only after MM pasted the matching confirm message.
        if (
            message.author.id == form["ticket_user_id"]
            and is_adder_confirm(message.content)
            and form.get("mm_confirm_sent")
        ):
            form["player_conf_pending"] = True
            if form.get("waiting_for_adder_confirm"):
                form["waiting_for_adder_confirm"] = False
                form["player_confirmed"] = True
                form.pop("player_conf_pending", None)
                await start_game_fn(message.channel, form, bot_user, bot)
                return

        if (
            form.get("waiting_for_confirm")
            and expected
            and message.content.strip() == expected.strip()
            and member_has_listen_role(message.author)
        ):
            form["game_confirmer_user_id"] = message.author.id
            form["mm_confirm_sent"] = True
            await reply_message(message, "conf")
            form["waiting_for_confirm"] = False
            form["waiting_for_adder_confirm"] = True
            if form.get("player_conf_pending"):
                form["waiting_for_adder_confirm"] = False
                form["player_confirmed"] = True
                form.pop("player_conf_pending", None)
                await start_game_fn(message.channel, form, bot_user, bot)
            return
