"""Time is injected, never read (invariant 8). Real clocks live outside the core."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from .timefmt import TimeError


class Clock(Protocol):
    def now(self) -> datetime: ...


class FixedClock:
    """A clock that only moves when told to. Used by tests and by replay."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise TimeError("FixedClock needs a timezone-aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise ValueError("time only moves forward")
        self._at = self._at + delta
