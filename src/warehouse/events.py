"""In-process pub/sub event bus. Every subsystem talks through it, so the log is a
full audit trail and handlers (agents, metrics) stay decoupled from producers."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Event:
    tick: int
    kind: str
    data: dict[str, Any]


Handler = Callable[[Event], None]


class EventBus:
    def __init__(self, history: int = 10_000) -> None:
        self.tick = 0
        self.log: deque[Event] = deque(maxlen=history)
        self.counts: dict[str, int] = defaultdict(int)
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, kind: str, handler: Handler) -> None:
        """Subscribe to one event kind, or "*" for everything."""
        self._subs[kind].append(handler)

    def publish(self, kind: str, **data: Any) -> Event:
        event = Event(self.tick, kind, data)
        self.log.append(event)
        self.counts[kind] += 1
        for handler in (*self._subs.get(kind, ()), *self._subs.get("*", ())):
            handler(event)
        return event

    def recent(self, limit: int = 50, kind: str | None = None) -> list[Event]:
        events = [e for e in self.log if kind is None or e.kind == kind]
        return events[-limit:]
