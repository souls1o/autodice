import asyncio
import time

# Discord ~5 msgs / 5s per channel. Keep each destination paced, but do not
# serialize every ticket / DM / autopost behind one global queue.
_DEST_INTERVAL = 0.2
_GLOBAL_MIN_GAP = 0.05

_queues = {}
_workers = {}
_global_lock = asyncio.Lock()
_next_global_ok = 0.0


async def _pace_global():
    global _next_global_ok
    async with _global_lock:
        wait = _next_global_ok - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _next_global_ok = time.monotonic() + _GLOBAL_MIN_GAP


async def _dest_worker(key):
    queue = _queues[key]
    while True:
        send_fn, future = await queue.get()
        try:
            await _pace_global()
            result = await send_fn()
            if not future.done():
                future.set_result(result)
        except Exception as exc:
            print(f"[send_queue] send failed ({key}): {exc}")
            if not future.done():
                future.set_exception(exc)
        finally:
            queue.task_done()
        await asyncio.sleep(_DEST_INTERVAL)


def _ensure_worker(key):
    worker = _workers.get(key)
    if worker is None or worker.done():
        _queues.setdefault(key, asyncio.Queue())
        _workers[key] = asyncio.create_task(_dest_worker(key))


def start_send_worker():
    """Workers start lazily per destination; kept for on_ready compatibility."""
    return


async def _enqueue(key, send_fn):
    _ensure_worker(key)
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    await _queues[key].put((send_fn, future))
    return await future


def _channel_key(channel):
    return getattr(channel, "id", None) or id(channel)


async def send_channel(channel, content=None, **kwargs):
    async def _do():
        if content is None:
            return await channel.send(**kwargs)
        return await channel.send(content, **kwargs)

    return await _enqueue(_channel_key(channel), _do)


async def reply_message(message, content, **kwargs):
    channel = getattr(message, "channel", None)
    return await _enqueue(_channel_key(channel), lambda: message.reply(content, **kwargs))


async def send_user(user, content, **kwargs):
    async def _do():
        return await user.send(content, **kwargs)

    return await _enqueue(f"dm:{getattr(user, 'id', id(user))}", _do)
