"""Thin Redis-list work queues.

SPEC section 3 is explicit: Redis lists plus small worker loops, no distributed task
framework. This module is the whole queueing layer.

`FakeQueue` mirrors the interface exactly so the full pipeline runs in fixture mode
with no Redis at all (SPEC section 4).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any, Protocol, runtime_checkable

from .config import (  # noqa: F401 - re-exported for callers
    Q_ANALYZE,
    Q_BIZCHECK,
    Q_FETCH,
    Q_FETCH_DIRECT,
    Q_NOTIFY,
    get_settings,
)
from .logging_setup import get_logger

log = get_logger(__name__)

__all__ = [
    "Q_ANALYZE",
    "Q_BIZCHECK",
    "Q_FETCH",
    "Q_FETCH_DIRECT",
    "Q_NOTIFY",
    "FakeQueue",
    "Queue",
    "WorkQueue",
    "get_queue",
    "reset_queues",
]

JsonDict = dict[str, Any]


@runtime_checkable
class Queue(Protocol):
    """The interface both WorkQueue and FakeQueue satisfy."""

    name: str

    def push(self, item: JsonDict) -> None: ...

    def push_many(self, items: list[JsonDict]) -> None: ...

    def pop_blocking(self, timeout: int = 5) -> JsonDict | None: ...

    def pop_batch(self, max_items: int, timeout_sec: float) -> list[JsonDict]: ...

    def depth(self) -> int: ...

    def clear(self) -> None: ...


def _encode(item: JsonDict) -> str:
    return json.dumps(item, separators=(",", ":"), default=str)


def _decode(payload: bytes | str | None, *, queue_name: str) -> JsonDict | None:
    if payload is None:
        return None
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        log.warning("queue.undecodable_payload", queue=queue_name)
        return None
    if not isinstance(decoded, dict):
        log.warning("queue.non_dict_payload", queue=queue_name, type=type(decoded).__name__)
        return None
    return decoded


class WorkQueue:
    """Redis-list backed FIFO queue carrying JSON dict payloads.

    Push appends to the tail (RPUSH), pop takes from the head (BLPOP/LPOP), so the
    queue is FIFO.
    """

    def __init__(self, name: str, redis_client: Any | None = None, url: str | None = None):
        self.name = name
        if redis_client is not None:
            self._redis = redis_client
        else:
            import redis  # imported lazily so fixture mode need not have a server

            self._redis = redis.Redis.from_url(url or get_settings().redis_url)

    # --- writes ---------------------------------------------------------------

    def push(self, item: JsonDict) -> None:
        self._redis.rpush(self.name, _encode(item))

    def push_many(self, items: list[JsonDict]) -> None:
        if not items:
            return
        self._redis.rpush(self.name, *[_encode(i) for i in items])

    # --- reads ----------------------------------------------------------------

    def pop_blocking(self, timeout: int = 5) -> JsonDict | None:
        """Block up to `timeout` seconds for one item. None on timeout."""
        result = self._redis.blpop([self.name], timeout=timeout)
        if result is None:
            return None
        _key, payload = result
        return _decode(payload, queue_name=self.name)

    def pop_batch(self, max_items: int, timeout_sec: float) -> list[JsonDict]:
        """Accumulate up to `max_items` or until `timeout_sec` elapses, whichever first.

        This backs SPEC 7.3's "50 user ids or 5 seconds". The first item is waited
        for with a blocking pop; subsequent ones are drained non-blockingly until
        either the batch is full or the deadline passes.
        """
        if max_items <= 0:
            return []

        batch: list[JsonDict] = []
        deadline = time.monotonic() + timeout_sec

        while len(batch) < max_items:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Redis BLPOP takes integer seconds; 0 means "block forever", which we
            # must never do, so clamp sub-second waits to a non-blocking LPOP.
            if remaining >= 1:
                item = self.pop_blocking(timeout=int(remaining))
            else:
                item = _decode(self._redis.lpop(self.name), queue_name=self.name)
            if item is None:
                if remaining < 1:
                    # Nothing left and no time to block again.
                    break
                continue
            batch.append(item)

        return batch

    # --- introspection --------------------------------------------------------

    def depth(self) -> int:
        return int(self._redis.llen(self.name))

    def clear(self) -> None:
        self._redis.delete(self.name)

    def __repr__(self) -> str:
        return f"WorkQueue(name={self.name!r})"


class FakeQueue:
    """In-memory queue with the identical interface, for fixture mode and tests.

    Thread-safe; `pop_blocking` uses a condition variable so batching semantics
    match the Redis implementation closely enough for the pipeline to behave the
    same way in either mode.
    """

    def __init__(self, name: str):
        self.name = name
        self._items: deque[JsonDict] = deque()
        self._cond = threading.Condition()

    def push(self, item: JsonDict) -> None:
        with self._cond:
            self._items.append(json.loads(_encode(item)))
            self._cond.notify()

    def push_many(self, items: list[JsonDict]) -> None:
        if not items:
            return
        with self._cond:
            for item in items:
                self._items.append(json.loads(_encode(item)))
            self._cond.notify_all()

    def pop_blocking(self, timeout: int = 5) -> JsonDict | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._items.popleft()

    def pop_batch(self, max_items: int, timeout_sec: float) -> list[JsonDict]:
        if max_items <= 0:
            return []
        batch: list[JsonDict] = []
        deadline = time.monotonic() + timeout_sec
        with self._cond:
            while len(batch) < max_items:
                if self._items:
                    batch.append(self._items.popleft())
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
        return batch

    def depth(self) -> int:
        with self._cond:
            return len(self._items)

    def clear(self) -> None:
        with self._cond:
            self._items.clear()

    def __repr__(self) -> str:
        return f"FakeQueue(name={self.name!r}, depth={self.depth()})"


# --- factory ------------------------------------------------------------------

_queues: dict[str, Queue] = {}
_queues_lock = threading.Lock()


def _redis_reachable(url: str) -> bool:
    try:
        import redis
    except ImportError:
        return False
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
        return True
    except Exception:
        return False


def get_queue(name: str) -> Queue:
    """Return a cached queue for `name`.

    Redis-backed when available. In fixture mode with no reachable Redis we fall
    back to FakeQueue so the whole pipeline still runs end to end (SPEC section 4).
    """
    with _queues_lock:
        existing = _queues.get(name)
        if existing is not None:
            return existing

        settings = get_settings()
        queue: Queue
        if settings.is_fixture_mode and not _redis_reachable(settings.redis_url):
            log.info("queue.using_fake", queue=name, reason="fixture_mode_no_redis")
            queue = FakeQueue(name)
        else:
            queue = WorkQueue(name, url=settings.redis_url)

        _queues[name] = queue
        return queue


def reset_queues() -> None:
    """Test hook - drop cached queue instances."""
    with _queues_lock:
        _queues.clear()
