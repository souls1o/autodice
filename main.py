import discord
from discord.ext import commands, tasks
import asyncio
import config
from forms import (
    build_dm_gamemodes_text,
    build_dm_help_text,
    handle_bot_added_to_channel,
    handle_form_step,
    handle_global_listeners,
    handle_ticket_command,
    is_roll_command,
    should_process_channel,
    start_ticket_form,
    was_bot_added_to_channel,
)
from games import DA_HOOD_BOT_ID, handle_da_hood_message, handle_user_roll, start_game
from message_queue import reply_message, send_channel, start_send_worker
from services import get_house_balance_text, build_stats_text, get_wallets
from state import (
    active_forms,
    clear_ticket_session,
    get_auto_post_channel_id,
    get_form,
    is_maintenance_mode,
    is_ticket_channel,
    set_auto_post_channel_id,
    toggle_maintenance,
)

bot = commands.Bot(command_prefix="!", self_bot=True)


def _find_lf_players_channel():
    target = config.AUTO_POST_CHANNEL_NAME.lower()
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name.lower() == target:
                return channel
    return None


async def resolve_auto_post_channel(*, force_search=False):
    channel_id = get_auto_post_channel_id()
    if not force_search:
        channel = bot.get_channel(channel_id)
        if channel is not None:
            return channel
        try:
            return await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden):
            pass
        except discord.HTTPException:
            pass

    channel = _find_lf_players_channel()
    if channel is None:
        return None

    if channel.id != channel_id:
        set_auto_post_channel_id(channel.id)
        print(f"[auto_post] switched to #{channel.name} (`{channel.id}`)")
    return channel


async def send_auto_post():
    channel = await resolve_auto_post_channel()
    if channel is None:
        print(f"[auto_post] no #{config.AUTO_POST_CHANNEL_NAME} channel found")
        return

    try:
        await send_channel(channel, config.AUTO_POST_MESSAGE)
        return
    except (discord.NotFound, discord.Forbidden):
        channel = await resolve_auto_post_channel(force_search=True)
        if channel is None:
            print(f"[auto_post] cannot post — #{config.AUTO_POST_CHANNEL_NAME} not found")
            return
        await send_channel(channel, config.AUTO_POST_MESSAGE)


def ensure_auto_post():
    if auto_post.is_running():
        return
    auto_post.start()
    print("[auto_post] task started")


@bot.event
async def on_ready():
    print(f"✅ Selfbot logged in as {bot.user} (ID: {bot.user.id})")
    start_send_worker()
    ensure_auto_post()
    if not watchdog.is_running():
        watchdog.start()


@bot.event
async def on_disconnect():
    print("⚠️ Disconnected from Discord gateway — reconnecting...")


@bot.event
async def on_resumed():
    print("✅ Session resumed")
    ensure_auto_post()


@tasks.loop(seconds=config.AUTO_POST_INTERVAL)
async def auto_post():
    try:
        if is_maintenance_mode():
            return
        await send_auto_post()
    except discord.HTTPException as exc:
        print(f"[auto_post] HTTP error ({exc.status}): {exc}")
    except Exception as exc:
        print(f"[auto_post] error: {exc}")


@auto_post.before_loop
async def before_auto_post():
    await bot.wait_until_ready()


@auto_post.error
async def auto_post_error(exc):
    print(f"[auto_post] task error: {exc}")


@tasks.loop(minutes=2)
async def watchdog():
    if not bot.is_ready():
        return
    if not auto_post.is_running():
        print("[watchdog] auto_post stopped — restarting")
        ensure_auto_post()


@watchdog.before_loop
async def before_watchdog():
    await bot.wait_until_ready()


@bot.event
async def on_guild_channel_create(channel):
    if isinstance(channel, discord.TextChannel) and was_bot_added_to_channel(channel, bot.user):
        await handle_bot_added_to_channel(bot, channel)


@bot.event
async def on_guild_channel_update(before, after):
    if isinstance(after, discord.TextChannel) and was_bot_added_to_channel(after, bot.user, before):
        await handle_bot_added_to_channel(bot, after)


@bot.event
async def on_guild_channel_delete(channel):
    if channel.id == get_auto_post_channel_id():
        print(f"[auto_post] channel deleted — will look for #{config.AUTO_POST_CHANNEL_NAME}")
    clear_ticket_session(channel.id)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    try:
        await _handle_message(message)
    except Exception as exc:
        print(f"[on_message] error in #{getattr(message.channel, 'name', '?')}: {exc}")


async def _handle_message(message: discord.Message):
    if isinstance(message.channel, discord.DMChannel):
        content = message.content.strip().lower()

        if content == "!help":
            await reply_message(message, build_dm_help_text(message.author.id))
            return
        if content == "!gamemodes":
            await reply_message(message, build_dm_gamemodes_text())
            return
        if content == "!housebal":
            await reply_message(message, await get_house_balance_text())
            return

        if content == "!stats" and message.author.id == config.ADMIN_USER_ID:
            await reply_message(message, await build_stats_text())
            return
        if message.content == "!wallet" and message.author.id == config.ADMIN_USER_ID:
            wallets = await get_wallets()
            lines = ["**Wallets:**"]
            for w in wallets.get("wallets", []):
                lines.append(f"**{w['currency'].upper()}:** `{w['address']}` | Balance: {w.get('balance', 0)}")
            await reply_message(message, "\n".join(lines))
            return
        if message.content.strip().lower() == "!toggle maintenance" and message.author.id == config.ADMIN_USER_ID:
            enabled = toggle_maintenance()
            status = "enabled" if enabled else "disabled"
            await reply_message(message, f"Maintenance mode is {status}.")
            return

    if not isinstance(message.channel, discord.TextChannel):
        return

    if not should_process_channel(message.channel, message, bot.user):
        return

    if is_ticket_channel(message.channel):
        if await handle_ticket_command(message, bot.user, bot):
            return

    channel_id = message.channel.id
    form = get_form(channel_id)

    if is_roll_command(message.content) and form and form.get("game_state", {}).get("game_type") == "dice":
        if message.author.id == form["ticket_user_id"]:
            await handle_user_roll(message, form, bot.user)
        return

    if form and "game_state" in form:
        state = form["game_state"]
        if state.get("game_type") == "dice" and message.author.bot and (
            state.get("waiting_for_embed")
            or state.get("pending_bot_total") is not None
            or state.get("awaiting_user_after_bot")
        ):
            await handle_da_hood_message(message, form, bot.user, bot)
            return
        if message.author.id == DA_HOOD_BOT_ID:
            await handle_da_hood_message(message, form, bot.user, bot)
            return

    form = get_form(channel_id)
    if form and not form.get("game_state") and not form.get("waiting_for_rerun") and not form.get("waiting_for_rerun_bet") and not form.get("waiting_for_confirm"):
        await handle_form_step(message, form, bot.user)

    if channel_id not in active_forms:
        await asyncio.sleep(1)
        await start_ticket_form(message.channel, bot.user, bot)
        return

    await handle_global_listeners(message, bot.user, start_game, bot)


if __name__ == "__main__":
    token = config.DISCORD_TOKEN
    if not token:
        token = input("Paste your Discord User Token: ")
    bot.run(token)
