import asyncio
import time

import discord

# Selfbots get global rate limits / guild timeouts across channels. One send at
# a time; retries wait out 429s and ~10m server timeouts so tickets resume.
_SEND_INTERVAL = 0.35
_MAX_RETRIES = 10
_BASE_BACKOFF = 1.5
_MAX_BACKOFF = 60.0

# Guild communication timeouts are often 10 minutes — keep trying through that window.
_TIMEOUT_MAX_RETRIES = 40
_TIMEOUT_POLL_SECONDS = 30.0
_TIMEOUT_MAX_WAIT = 12 * 60  # 12 minutes hard cap per send

_queue = asyncio.Queue()
_worker_task = None
_cooldown_until = 0.0
_send_interval = _SEND_INTERVAL


def _retry_after_seconds(exc):
    """Best-effort Retry-After from a Discord HTTP error."""
    retry = getattr(exc, "retry_after", None)
    if retry is not None:
        try:
            return max(float(retry), 0.5)
        except (TypeError, ValueError):
            pass

    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is not None:
            try:
                return max(float(raw), 0.5)
            except (TypeError, ValueError):
                pass

    return None


def _error_text(exc):
    return (str(exc) or "").lower()


def _is_guild_timeout_error(exc):
    """True when Discord is blocking sends due to a server communication timeout."""
    if not isinstance(exc, discord.HTTPException):
        return False
    text = _error_text(exc)
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)

    if any(
        needle in text
        for needle in (
            "timed out",
            "communication disabled",
            "temporarily restricted",
            "you are muted",
            "cannot send messages",
        )
    ):
        # Permanent DM privacy failures use similar wording — those are 50007.
        if code in (50007, 50278):
            return False
        return True

    # Timed-out members often get Missing Permissions (50013) / Forbidden in guild channels.
    if status in (400, 403) and code in (50013, 40001):
        return True
    return False


def _is_retryable_send_error(exc):
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, discord.RateLimited):
        return True
    if _is_guild_timeout_error(exc):
        return True
    if not isinstance(exc, discord.HTTPException):
        return False
    status = getattr(exc, "status", None)
    if status in (408, 429, 500, 502, 503, 504):
        return True
    text = _error_text(exc)
    return any(
        needle in text
        for needle in (
            "rate limit",
            "ratelimit",
            "too many requests",
            "temporarily",
            "cloudflare",
            "try again",
        )
    )


async def _wait_global_cooldown():
    global _cooldown_until
    wait = _cooldown_until - time.monotonic()
    if wait > 0:
        print(f"[send_queue] cooling down {wait:.1f}s — tickets will resume after")
        await asyncio.sleep(wait)


def _arm_cooldown(seconds):
    global _cooldown_until, _send_interval
    seconds = max(float(seconds), 0.5)
    _cooldown_until = max(_cooldown_until, time.monotonic() + seconds)
    _send_interval = min(max(_send_interval * 1.5, _SEND_INTERVAL), 2.0)


def _ease_interval():
    global _send_interval
    if _send_interval > _SEND_INTERVAL:
        _send_interval = max(_SEND_INTERVAL, _send_interval * 0.85)


async def _run_send_with_retries(send_fn):
    last_exc = None
    started = time.monotonic()
    attempt = 0
    max_attempts = _MAX_RETRIES

    while attempt < max_attempts:
        await _wait_global_cooldown()
        try:
            result = await send_fn()
            _ease_interval()
            return result
        except Exception as exc:
            last_exc = exc
            if not _is_retryable_send_error(exc):
                raise

            is_timeout = _is_guild_timeout_error(exc)
            if is_timeout:
                max_attempts = _TIMEOUT_MAX_RETRIES
                if (time.monotonic() - started) >= _TIMEOUT_MAX_WAIT:
                    raise

            if attempt >= max_attempts - 1:
                raise

            retry_after = _retry_after_seconds(exc)
            if retry_after is None:
                if is_timeout:
                    # Poll every 30s through a typical 10m guild timeout.
                    retry_after = _TIMEOUT_POLL_SECONDS
                else:
                    retry_after = min(_BASE_BACKOFF * (2 ** attempt), _MAX_BACKOFF)
            else:
                # Allow a full guild timeout Retry-After (e.g. 600s).
                cap = 15 * 60 if is_timeout else 120.0
                retry_after = min(max(retry_after, 0.5), cap)

            _arm_cooldown(retry_after)
            status = getattr(exc, "status", "?")
            kind = "guild timeout" if is_timeout else "rate-limited/temp fail"
            print(
                f"[send_queue] {kind} (status={status}) "
                f"— retry {attempt + 1}/{max_attempts} in {retry_after:.1f}s; "
                f"active tickets stay queued"
            )
            await asyncio.sleep(retry_after)
            attempt += 1

    raise last_exc


async def _send_worker():
    while True:
        send_fn, future = await _queue.get()
        try:
            result = await _run_send_with_retries(send_fn)
            if not future.done():
                future.set_result(result)
        except Exception as exc:
            print(f"[send_queue] send failed after retries: {exc}")
            if not future.done():
                future.set_exception(exc)
        finally:
            _queue.task_done()
        await asyncio.sleep(_send_interval)


def start_send_worker():
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_send_worker())


async def _enqueue(send_fn):
    start_send_worker()
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await _queue.put((send_fn, future))
    return await future


async def send_channel(channel, content=None, **kwargs):
    async def _do():
        if content is None:
            return await channel.send(**kwargs)
        return await channel.send(content, **kwargs)

    return await _enqueue(_do)


async def reply_message(message, content, **kwargs):
    return await _enqueue(lambda: message.reply(content, **kwargs))


async def send_user(user, content, **kwargs):
    async def _do():
        return await user.send(content, **kwargs)

    return await _enqueue(_do)
