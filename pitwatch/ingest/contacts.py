"""A contact change, and the clock that decides one is real.

These lived in the reader for the panel module. They are not about that module,
or about MQTT: an edge is an edge and a bounce is a bounce, so they sit on their
own and the reader imports them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IoEvent:
    """One contact changing, after debounce and after inversion."""

    ts: datetime
    channel: int
    # What the input was called when this was recorded. The channel is the
    # identity; this is here so old history still reads after a relabel.
    label: str
    state: bool
    raw: bool


class Debouncer:
    """Holds a channel's state until it has lasted long enough to count.

    One instance per reader, tracking all eight channels against the one hold
    from the settings. Each channel keeps its own candidate, so a float
    bouncing on DI1 does not delay a contact settling on DI2.

    Different in shape from the polled version this replaces. There, a change
    was confirmed by the next poll agreeing; here nothing arrives unless
    something changes, so a candidate is confirmed by the clock rather than by
    another message. The hold is a constructor argument so the tests can set it
    to zero and drive transitions directly.
    """

    def __init__(self, hold_ms: int) -> None:
        self._hold = hold_ms / 1000.0
        self._stable: dict[int, bool] = {}
        self._candidate: dict[int, tuple[bool, float]] = {}

    def stable_state(self, channel: int) -> bool | None:
        return self._stable.get(channel)

    def prime(self, channel: int, raw: bool) -> None:
        """Set a channel's state without treating it as a change."""
        self._stable[channel] = raw
        self._candidate.pop(channel, None)

    def feed(self, channel: int, raw: bool, now: float) -> bool | None:
        """Offer a reading. Returns the new stable state, or None if unchanged.

        ``now`` is a monotonic clock reading, passed in rather than read here so
        that the tests do not have to sleep to exercise a hold.
        """
        stable = self._stable.get(channel)
        if raw == stable:
            # Back where it started, so whatever was pending was a bounce.
            self._candidate.pop(channel, None)
            return None

        started = self._candidate.get(channel)
        if started is None or started[0] != raw:
            self._candidate[channel] = (raw, now)
            started = self._candidate[channel]

        if now - started[1] < self._hold:
            return None

        self._candidate.pop(channel, None)
        self._stable[channel] = raw
        return raw

    def next_deadline(self, now: float) -> float | None:
        """Seconds until the earliest candidate has lasted the hold.

        None when nothing is waiting. This is what lets a change be confirmed
        by the clock, which is the whole premise of debouncing a source that
        only speaks when something changes.
        """
        if not self._candidate:
            return None
        earliest = min(started for _, started in self._candidate.values())
        return max(0.0, self._hold - (now - earliest))

    def settled(self, now: float) -> dict[int, bool]:
        """Every candidate that has now lasted the hold, promoted to stable.

        The counterpart to feed. feed answers "did this message settle
        anything"; this answers "did the passage of time settle anything", and
        without the second one a change that arrives once and is never
        contradicted waits forever.
        """
        done: dict[int, bool] = {}
        for channel, (raw, started) in list(self._candidate.items()):
            if now - started >= self._hold:
                del self._candidate[channel]
                self._stable[channel] = raw
                done[channel] = raw
        return done
