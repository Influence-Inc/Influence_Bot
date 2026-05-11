"""
In-process pub/sub for chat-space real-time events.

Each gunicorn worker holds its own subscribers — because the bot runs with
`--workers 1`, every open SSE connection lives in the same process, so a
plain `dict[space_id, set[Queue]]` is enough.

If we ever need to scale beyond a single worker, swap this module for a
Redis-backed pub/sub (same `publish` / `subscribe` interface).

Event shape:
    {"type": "message" | "reaction" | "read" | "typing" | "ping",
     "space_id": int,
     "data": {...},
     "ts": ISO-8601 string}
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# space_id -> set of subscriber Queues
_subscribers: dict[int, set["queue.Queue[dict]"]] = {}
_lock = threading.Lock()


# Soft-limit how many open subscribers we allow per space. Each one ties up
# one gthread for the duration of the connection.
MAX_SUBSCRIBERS_PER_SPACE = 32


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish(space_id: int, event_type: str, data: dict[str, Any]) -> int:
    """
    Push an event to every subscriber of `space_id`. Returns the number of
    subscribers the event was delivered to.
    """
    payload = {"type": event_type, "space_id": space_id, "data": data, "ts": _now_iso()}
    with _lock:
        subs = list(_subscribers.get(space_id, ()))
    delivered = 0
    for q in subs:
        try:
            q.put_nowait(payload)
            delivered += 1
        except queue.Full:
            # Slow consumer — drop the event for this subscriber rather than
            # block the publisher. The next poll/SSE reconnect will catch
            # them up via the `since` parameter.
            logger.warning(
                "chat_pubsub: subscriber queue full for space=%s; dropping event",
                space_id,
            )
    return delivered


class _Subscription:
    """Context-manager-friendly subscriber handle."""

    def __init__(self, space_id: int) -> None:
        self.space_id = space_id
        self.q: "queue.Queue[dict]" = queue.Queue(maxsize=256)

    def __enter__(self) -> "_Subscription":
        with _lock:
            bucket = _subscribers.setdefault(self.space_id, set())
            if len(bucket) >= MAX_SUBSCRIBERS_PER_SPACE:
                # Refuse the connection at the caller layer.
                raise OverflowError("too many subscribers for this space")
            bucket.add(self.q)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        with _lock:
            bucket = _subscribers.get(self.space_id)
            if bucket is not None:
                bucket.discard(self.q)
                if not bucket:
                    _subscribers.pop(self.space_id, None)

    def iter_events(self, *, heartbeat_seconds: float = 25.0) -> Iterator[dict]:
        """
        Yield events as they arrive. Emits a `ping` heartbeat every
        `heartbeat_seconds` so reverse proxies (Railway, browsers) don't
        sever the connection on idle. Returns when the caller breaks out
        (e.g. the client disconnects and the WSGI server raises).
        """
        last_ping = time.monotonic()
        while True:
            timeout = max(0.1, heartbeat_seconds - (time.monotonic() - last_ping))
            try:
                event = self.q.get(timeout=timeout)
                yield event
            except queue.Empty:
                last_ping = time.monotonic()
                yield {"type": "ping", "space_id": self.space_id, "data": {}, "ts": _now_iso()}


def subscribe(space_id: int) -> _Subscription:
    """Open a new subscription. Use as a `with` block."""
    return _Subscription(space_id)


# ---------------------------------------------------------------------------
# Typing indicator: ephemeral, lives entirely in memory.
#
# We don't persist typing state, but we do dedupe rapid pings — clients send
# /typing every ~2s while a user types; we publish at most once per ~1.5s per
# (space, party, identifier) so SSE traffic stays light.
# ---------------------------------------------------------------------------

_TYPING_PING_INTERVAL = 1.5
_typing_last_ping: dict[tuple[int, str, str], float] = {}
_typing_lock = threading.Lock()


def maybe_publish_typing(
    *, space_id: int, party: str, identifier: str, display_name: Optional[str]
) -> bool:
    key = (space_id, party, identifier)
    now = time.monotonic()
    with _typing_lock:
        last = _typing_last_ping.get(key, 0.0)
        if now - last < _TYPING_PING_INTERVAL:
            return False
        _typing_last_ping[key] = now
    publish(
        space_id,
        "typing",
        {"party": party, "identifier": identifier, "display_name": display_name},
    )
    return True
