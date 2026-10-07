"""Live event log of a running investigation (the UI long-polls it to watch the graph grow)."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any


class EventLog:
    def __init__(self, maxlen: int = 60000) -> None:
        self._events: deque[tuple[int, str, Any]] = deque(maxlen=maxlen)
        self.seq = 0
        self._cond = asyncio.Condition()
        self._pending = False

    def emit(self, kind: str, payload: Any) -> None:
        self.seq += 1
        self._events.append((self.seq, kind, payload))
        self._pending = True

    async def notify(self) -> None:
        if self._pending:
            self._pending = False
            async with self._cond:
                self._cond.notify_all()

    def since(self, after: int, limit: int = 5000) -> dict:
        oldest = self._events[0][0] if self._events else self.seq + 1
        if after and after < oldest - 1:
            return {"resync": True, "seq": self.seq, "events": []}
        out = [{"seq": s, "kind": k, "data": d} for s, k, d in self._events if s > after][:limit]
        return {"resync": False, "seq": out[-1]["seq"] if out else max(after, 0) if after <= self.seq else self.seq,
                "events": out}

    async def wait(self, after: int, timeout: float) -> dict:
        if self.seq <= after:
            try:
                async with self._cond:
                    await asyncio.wait_for(self._cond.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return self.since(after)
