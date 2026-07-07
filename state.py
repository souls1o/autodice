active_forms = {}
ticket_channels = set()
ticket_sessions = {}
maintenance_mode = False
maintenance_notified_channels = set()
testing_mode = False


def is_maintenance_mode():
    return maintenance_mode


def is_testing_mode():
    return testing_mode


def toggle_maintenance():
    global maintenance_mode
    maintenance_mode = not maintenance_mode
    if not maintenance_mode:
        maintenance_notified_channels.clear()
    return maintenance_mode


def toggle_testing():
    global testing_mode
    testing_mode = not testing_mode
    return testing_mode


async def notify_maintenance(channel):
    if channel.id in maintenance_notified_channels:
        return
    maintenance_notified_channels.add(channel.id)
    try:
        await channel.send("🚧 **MAINTENANCE MODE IS ENABLED** 🚧")
    except Exception:
        maintenance_notified_channels.discard(channel.id)


def register_ticket_channel(channel_id):
    if channel_id in ticket_channels:
        return False
    ticket_channels.add(channel_id)
    return True


def unregister_ticket_channel(channel_id):
    ticket_channels.discard(channel_id)


def get_ticket_session(channel_id):
    return ticket_sessions.setdefault(channel_id, {
        "ticket_user_id": None,
        "winnings_usd": 0.0,
        "winnings_crypto": 0.0,
        "winnings_coin": "ltc",
    })


def save_session_from_form(channel_id, form):
    if not form:
        return
    session = get_ticket_session(channel_id)
    if form.get("ticket_user_id"):
        session["ticket_user_id"] = form["ticket_user_id"]
    session["winnings_usd"] = form.get("winnings_usd", 0.0)
    session["winnings_crypto"] = form.get("winnings_crypto", 0.0)
    session["winnings_coin"] = form.get("winnings_coin", session.get("winnings_coin", "ltc"))
    session["total_wagered_usd"] = form.get("total_wagered_usd", 0.0)
    if form.get("funds_recipient_id"):
        session["funds_recipient_id"] = form["funds_recipient_id"]
    if form.get("payout_address"):
        session["payout_address"] = form["payout_address"]
    if form.get("game_started"):
        session["game_started"] = True


def can_cancel_ticket(channel_id, form=None):
    form = form or active_forms.get(channel_id)
    if form and (form.get("game_started") or form.get("game_state")):
        return False
    session = ticket_sessions.get(channel_id, {})
    if session.get("game_started"):
        return False
    return True


def apply_session_to_form(channel_id, form):
    session = get_ticket_session(channel_id)
    if session.get("ticket_user_id"):
        form["ticket_user_id"] = session["ticket_user_id"]
    form["winnings_usd"] = session.get("winnings_usd", 0.0)
    form["winnings_crypto"] = session.get("winnings_crypto", 0.0)
    form["winnings_coin"] = session.get("winnings_coin", "ltc")


def get_hold_data(channel_id):
    form = active_forms.get(channel_id)
    if form:
        return (
            form.get("winnings_usd", 0.0),
            form.get("winnings_crypto", 0.0),
            form.get("winnings_coin", "ltc"),
        )
    session = get_ticket_session(channel_id)
    return (
        session.get("winnings_usd", 0.0),
        session.get("winnings_crypto", 0.0),
        session.get("winnings_coin", "ltc"),
    )


def new_form_dict(channel_id, ticket_user_id):
    session = get_ticket_session(channel_id)
    if ticket_user_id:
        session["ticket_user_id"] = ticket_user_id
    return {
        "ticket_user_id": ticket_user_id or session.get("ticket_user_id"),
        "step": 0,
        "responses": {},
        "waiting_for_address": False,
        "waiting_for_confirm": False,
        "winnings_usd": session.get("winnings_usd", 0.0),
        "winnings_crypto": session.get("winnings_crypto", 0.0),
        "winnings_coin": session.get("winnings_coin", "ltc"),
        "total_wagered_usd": session.get("total_wagered_usd", 0.0),
    }


def is_ticket_channel(channel):
    if channel.id in ticket_channels or channel.id in active_forms or channel.id in ticket_sessions:
        return True
    return "ticket" in channel.name.lower()


def get_form(channel_id):
    return active_forms.get(channel_id)


def cancel_rerun_timeout(form):
    if not form:
        return
    task = form.pop("rerun_timeout_task", None)
    if task and not task.done():
        task.cancel()


def cancel_active_form(channel, form=None):
    cancel_rerun_timeout(form)
    if form:
        save_session_from_form(channel.id, form)
    active_forms.pop(channel.id, None)


def clear_ticket_session(channel_id):
    cancel_rerun_timeout(active_forms.get(channel_id))
    active_forms.pop(channel_id, None)
    ticket_sessions.pop(channel_id, None)
    ticket_channels.discard(channel_id)


def finish_form(channel, form, *, payout=False):
    cancel_rerun_timeout(form)
    channel_id = channel.id
    if payout:
        clear_ticket_session(channel_id)
    else:
        save_session_from_form(channel_id, form)
        active_forms.pop(channel_id, None)
