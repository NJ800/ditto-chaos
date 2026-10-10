"""In-process event bus behind the dashboard's live stream (GET /api/stream).

Every event is {v, boot, seq, ts, type, data}. `seq` increases by one per event within
one process lifetime (`boot` changes on restart, so a client can tell a reset from a gap).
A bounded ring of recent events lets a reconnecting client replay what it missed; when it
has fallen further behind than the ring, or its own queue overflowed, it is told to
`resync` (re-fetch the REST snapshot) instead of being fed a silently incomplete stream.

Publishing never blocks and never waits for a subscriber: a slow dashboard cannot slow
down, or stall, the experiment controller.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from typing import AsyncIterator

SCHEMA_VERSION = 1


class _Subscriber:
    def __init__(self, loop: asyncio.AbstractEventLoop, size: int):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=size)
        self.overflowed = False

    def offer(self, event: dict) -> None:  # runs on the subscriber's own loop
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True


class EventBus:
    def __init__(self, history: int = 1500, queue_size: int = 300):
        self.boot = uuid.uuid4().hex[:12]
        self._seq = 0
        self._history: deque[dict] = deque(maxlen=history)
        self._subs: set[_Subscriber] = set()
        self._lock = threading.Lock()
        self._queue_size = queue_size
        self.dropped_overflows = 0

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, type_: str, data: dict) -> dict:
        """Safe to call from any thread, with or without a running event loop."""
        with self._lock:
            self._seq += 1
            event = {"v": SCHEMA_VERSION, "boot": self.boot, "seq": self._seq,
                     "ts": round(time.time(), 3), "type": type_, "data": data}
            self._history.append(event)
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(sub.offer, event)
            except RuntimeError:      # that subscriber's loop is already closed
                self._discard(sub)
        return event

    def _discard(self, sub: _Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)

    def subscriber_count(self) -> int:
        return len(self._subs)

    def stats(self) -> dict:
        return {"boot": self.boot, "seq": self._seq, "subscribers": len(self._subs),
                "buffered": len(self._history), "overflows": self.dropped_overflows}

    def _resync(self, reason: str) -> dict:
        return {"v": SCHEMA_VERSION, "boot": self.boot, "seq": self._seq, "ts": round(time.time(), 3),
                "type": "resync", "data": {"reason": reason}}

    async def stream(self, last_seq: int | None = None, boot: str | None = None,
                     heartbeat_s: float = 15.0) -> AsyncIterator[dict]:
        sub = _Subscriber(asyncio.get_running_loop(), self._queue_size)
        with self._lock:
            self._subs.add(sub)
            backlog = list(self._history)
        try:
            if last_seq is not None:
                oldest = backlog[0]["seq"] if backlog else self._seq + 1
                if (boot is not None and boot != self.boot) or last_seq > self._seq:
                    yield self._resync("server restarted")
                elif last_seq + 1 < oldest:
                    yield self._resync("missed more events than the replay buffer holds")
                else:
                    for event in backlog:
                        if event["seq"] > last_seq:
                            yield event
                    last_seq = backlog[-1]["seq"] if backlog else last_seq
            sent = last_seq or 0
            while True:
                try:
                    event = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat_s)
                except asyncio.TimeoutError:
                    yield {"v": SCHEMA_VERSION, "boot": self.boot, "seq": self._seq,
                           "ts": round(time.time(), 3), "type": "heartbeat", "data": {}}
                    continue
                if sub.overflowed:
                    sub.overflowed = False
                    self.dropped_overflows += 1
                    while not sub.queue.empty():
                        sub.queue.get_nowait()
                    # `sent` is fixed BEFORE yielding: anything published while the client
                    # handles the resync must still be delivered afterwards.
                    resync = self._resync("this client fell behind and events were dropped")
                    sent = resync["seq"]
                    yield resync
                    continue
                if event["seq"] <= sent:
                    continue      # already delivered through the replayed backlog
                sent = event["seq"]
                yield event
        finally:
            self._discard(sub)


bus = EventBus()
