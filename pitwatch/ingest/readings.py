"""One reading from one clamp.

A current at a moment, filed under the channel it was recorded against, which
is the shape everything downstream stores and reads regardless of what
published it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EmSample:
    """One reading from one clamp: when, which channel, how many amps.

    Amps and nothing else. A clamp source reads one number at one path, and
    voltage is not it: a meter's own supply is not necessarily the phase its
    clamps are around, so its voltage belongs to a different circuit and
    everything derived from it, watts and power factor, inherits that. Current
    does not care what the meter is plugged into.
    """

    ts: datetime
    channel: int
    current: float | None
