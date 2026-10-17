"""In-process pub/sub so the dashboard can watch the pipeline live over SSE.

Bounded per-subscriber queues: a slow browser tab drops frames instead of
back-pressuring the pipeline. Observability must never slow the hot path.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

_MAX_QUEUE = 256


class EventBus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._recent: list[dict[str, Any]] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        evt = {"kind": kind, "ts": time.time(), **payload}
        self._recent.append(evt)
        if len(self._recent) > 300:
            self._recent = self._recent[-300:]
        for q in list(self._subs):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()       # drop oldest frame
                    q.put_nowait(evt)
                except Exception:
                    pass

    def recent(self, limit: int = 60) -> list[dict[str, Any]]:
        return self._recent[-limit:]

    @staticmethod
    def sse(evt: dict[str, Any]) -> str:
        return f"event: {evt.get('kind','message')}\ndata: {json.dumps(evt)}\n\n"


bus = EventBus()
