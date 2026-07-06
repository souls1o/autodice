import discord
from discord.ext import commands, tasks
import asyncio
import os
import re
import config

bot = commands.Bot(command_prefix='!', self_bot=True)

active_forms = {}

# ====================== VALIDATORS ======================
def bet_validator(response: str, form=None) -> bool:
    parts = response.strip().split()
    if len(parts) != 2:
        return False
    try:
        amount = int(parts[0])
    except ValueError:
        return False
    game = form.get("responses", {}).get("game") if form else None
    max_bet = 50 if game == "dice" else 65
    return 5 <= amount <= max_bet

VALIDATORS = {
    "bet_validator": bet_validator
}

# ====================== SOLANA ======================
try:
    from solana.rpc.async_api import AsyncClient
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import transfer, TransferParams
    from solders.transaction import VersionedTransaction
    from solders.message import MessageV0
    sol_client = AsyncClient(config.SOLANA_RPC_URL)
except ImportError:
    print("❌ Run: pip install solana solders")
    sol_client = None

async def send_emoji_msg(channel, content: str):
    await channel.send(content + " ✨")

# ====================== EVENTS ======================
@bot.event
async def on_ready():
    print(f'✅ Selfbot logged in as {bot.user} (ID: {bot.user.id})')
    auto_post.start()

@tasks.loop(seconds=config.AUTO_POST_INTERVAL)
async def auto_post():
    channel = bot.get_channel(config.AUTO_POST_CHANNEL_ID)
    if channel:
        await send_emoji_msg(channel, config.AUTO_POST_MESSAGE)

# ====================== TICKET DETECTION ======================
@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    ch_id = message.channel.id
    form = active_forms.get(ch_id)

    if form:
        await handle_form_step(message, form)
        return

    if "ticket" in message.channel.name.lower() and ch_id not in active_forms:
        await asyncio.sleep(4)
        await start_ticket_form(message.channel)
        return

    await handle_global_listeners(message)

async def start_ticket_form(channel):
    print(f"🔍 Scanning ticket channel: {channel.name}")
    ticket_user_id = None
    async for msg in channel.history(limit=30):
        if f"<@{bot.user.id}>" in msg.content or f"<@!{bot.user.id}>" in msg.content:
            ticket_user_id = msg.author.id
            break
    if not ticket_user_id:
        async for msg in channel.history(limit=30):
            if not msg.author.bot:
                ticket_user_id = msg.author.id
                break

    await send_emoji_msg(channel, "👋 Hello! Starting form...")

    active_forms[channel.id] = {
        "ticket_user_id": ticket_user_id,
        "step": 0,
        "responses": {},
        "waiting_for_address": False,
        "waiting_for_confirm": False
    }
    await ask_next_step(channel)

def get_dynamic_values(form):
    responses = form.get("responses", {})
    game = responses.get("game")
    max_bet = 65 if game == "dice" else 200
    return {"max_bet": max_bet}

def calculate_my_bet(form):
    responses = form.get("responses", {})
    bet_str = responses.get("bet", "0")
    try:
        his_bet = float(bet_str.split()[0])
    except:
        his_bet = 0.0

    game = responses.get("game")
    first_to = responses.get("first_to")
    if game == "coinflip":
        return round(his_bet * 0.85, 2)
    elif game == "dice":
        gamemode = responses.get("gamemode")
        if gamemode == "7s" and first_to == "ft3":
            return round(his_bet * 2.5, 2)
        elif (gamemode == "7s" and first_to == "ft5") or (gamemode == "7s_ties" and first_to == "ft3"):
            return round(his_bet * 3.5, 2)
        elif gamemode == "7s_ties" and first_to == "ft5":
            return round(his_bet * 4, 2)
        elif gamemode == "ties" and first_to == "ft3":
            return round(his_bet * 1.1, 2)
        elif gamemode == "ties" and first_to == "ft3":
            return round(his_bet * 1.25, 2)
        elif gamemode == "fair":
            return round(his_bet * 0.85, 2)
    return his_bet

def format_text(text: str, mention: str, responses: dict, dynamic=None) -> str:
    if dynamic is None:
        dynamic = {}
    result = text.replace("@mention", mention)
    for key, value in responses.items():
        result = result.replace(f"{{{key}}}", str(value))
    for key, value in dynamic.items():
        result = result.replace(f"{{{key}}}", str(value))
    return result

def should_show_question(q, responses):
    if "only_for" not in q:
        return True
    game = responses.get("game")
    return not game or game in q["only_for"]

async def ask_next_step(channel):
    form = active_forms.get(channel.id)
    if not form:
        return

    while form["step"] < len(config.FORM_QUESTIONS):
        q = config.FORM_QUESTIONS[form["step"]]
        if should_show_question(q, form["responses"]):
            break
        form["step"] += 1

    if form["step"] >= len(config.FORM_QUESTIONS):
        await finish_form(channel, form)
        return

    q = config.FORM_QUESTIONS[form["step"]]
    ticket_user = channel.guild.get_member(form["ticket_user_id"])
    mention = ticket_user.mention if ticket_user else f"<@{form['ticket_user_id']}>"

    dynamic = get_dynamic_values(form)
    question_text = format_text(q.get("text", ""), mention, form.get("responses", {}), dynamic)

    if q["type"] == "choice":
        await send_emoji_msg(channel, f"**Q{form['step']+1}:** {question_text}\nReply with a valid option.")
    elif q["type"] == "open":
        await send_emoji_msg(channel, f"**Q{form['step']+1}:** {question_text}")
    else:
        # Special handling for listen_address
        if q["type"] == "listen_address":
            coin = form["responses"].get("bet", "").split()[-1] if "bet" in form["responses"] else "sol"
            my_bet = calculate_my_bet(form)
            his_bet = form["responses"].get("bet", "unknown").split()[0] if "bet" in form["responses"] else "unknown"
            dynamic.update({"coin": coin, "my_bet": my_bet, "his_bet": his_bet})
            question_text = format_text(q.get("text", ""), mention, form.get("responses", {}), dynamic)
            form["waiting_for_address"] = True

        await send_emoji_msg(channel, question_text)
        if q["type"] == "listen_confirm":
            form["waiting_for_confirm"] = True

# ====================== FORM HANDLING ======================
async def handle_form_step(message: discord.Message, form):
    if form["ticket_user_id"] != message.author.id:
        return

    q = config.FORM_QUESTIONS[form["step"]]
    response = message.content.strip()
    upper_resp = response.upper()

    if q["type"] == "choice":
        for output_value, input_list in q["mapping"].items():
            if any(upper_resp == inp.upper() for inp in input_list):
                short_key = q.get("short_key")
                if short_key:
                    form["responses"][short_key] = output_value
                form["step"] += 1
                await ask_next_step(message.channel)
                return
        return  # silently ignore

    elif q["type"] == "open":
        validator_name = q.get("validator")
        validator = VALIDATORS.get(validator_name)
        if validator and not validator(response, form):
            await message.reply("❌ Invalid format or out of range.")
            return

        short_key = q.get("short_key")
        if short_key:
            form["responses"][short_key] = response
        form["step"] += 1
        await ask_next_step(message.channel)

# ====================== GLOBAL LISTENERS ======================
async def handle_global_listeners(message: discord.Message):
    form = active_forms.get(message.channel.id)
    if not form:
        return

    if form.get("waiting_for_address") and message.author.bot and "dyno" in message.author.name.lower():
        match = re.search(r'([1-9A-HJ-NP-Za-km-z]{32,44})', message.content)
        if match:
            await process_solana_transfer(message.channel, match.group(1), form)
            form["waiting_for_address"] = False
            form["step"] += 1
            await ask_next_step(message.channel)

    if form.get("waiting_for_confirm"):
        ticket_uid = form["ticket_user_id"]
        if (f"<@{bot.user.id}>" in message.content and f"<@{ticket_uid}>" in message.content):
            approver_role = discord.utils.get(message.guild.roles, id=config.APPROVER_ROLE_ID)
            if approver_role and approver_role in message.author.roles:
                await message.reply("confirm")
                form["waiting_for_confirm"] = False
                form["step"] += 1
                await ask_next_step(message.channel)

# ====================== SOLANA ======================
async def process_solana_transfer(channel, recipient_str: str, form):
    if not sol_client:
        await channel.send("❌ Solana not available.")
        return
    try:
        privkey = os.getenv("SOLANA_PRIVATE_KEY")
        if not privkey:
            await channel.send("❌ Set SOLANA_PRIVATE_KEY.")
            return
        keypair = Keypair.from_base58_string(privkey)
        recipient = Pubkey.from_string(recipient_str)
        amount = 100_000_000  # Customize

        ix = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=recipient, lamports=amount))
        latest = await sol_client.get_latest_blockhash()
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=[ix],
            address_lookup_table_accounts=[],
            recent_blockhash=latest.value.blockhash
        )
        tx = VersionedTransaction(msg, [keypair])
        result = await sol_client.send_raw_transaction(tx.serialize())

        await send_emoji_msg(channel, f"💸 Sent to `{recipient_str}` | Tx: https://solscan.io/tx/{result.value}")
    except Exception as e:
        await channel.send(f"❌ Transfer error: {e}")

async def finish_form(channel, form):
    ticket_user = channel.guild.get_member(form["ticket_user_id"])
    mention = ticket_user.mention if ticket_user else f"<@{form['ticket_user_id']}>"
    
    final_msg = config.FINAL_MESSAGE_TEMPLATE.format(
        mention=mention,
        **form.get("responses", {})
    )
    await send_emoji_msg(channel, final_msg)
    active_forms.pop(channel.id, None)

if __name__ == "__main__":
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        token = input("Paste your Discord User Token: ")
    bot.run(token)