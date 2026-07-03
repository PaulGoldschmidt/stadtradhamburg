"""A tiny thread-safe TTL cache. Wraps the (synchronous) query functions so
repeat/concurrent requests are served from memory and never re-hit DuckDB within
the TTL window — the main lever keeping CPU flat under public traffic."""

from __future__ import annotations

import functools
import threading
import time
from typing import Any, Callable


def ttl_cache(ttl_seconds: int) -> Callable:
    def decorator(fn: Callable) -> Callable:
        store: dict[Any, tuple[float, Any]] = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit is not None and now - hit[0] < ttl_seconds:
                    return hit[1]
            # Compute outside the lock; a rare duplicate compute on a cold key is
            # cheaper than serializing every caller.
            value = fn(*args, **kwargs)
            with lock:
                store[key] = (time.monotonic(), value)
            return value

        wrapper.cache_clear = store.clear  # type: ignore[attr-defined]
        return wrapper

    return decorator
