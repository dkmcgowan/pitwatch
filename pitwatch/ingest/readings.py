"""One reading from one clamp.

Lived in the reader for one brand of meter, which is not what it is about. A
current at a moment, filed under the channel it was recorded against, is the
shape everything downstream stores and reads regardless of what published it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EmSample:
    """One reading from one clamp: when, which channel, how many amps.

    There were five more fields here, voltage and real power and apparent
    power and power factor and frequency, which were the field names of one
    meter's status frame. They have been written as NULL on every row since a
    clamp source became a topic and a path, because a path reads one number
    and the number a pump monitor wants is the current. Five fields carried
    through a dataclass, a merge, an insert and a prime query to store nothing
    is the shape of the old design left behind in the new one.

    Voltage is the one worth saying why about. A meter's own supply is not
    necessarily the phase the clamps are around, so its voltage belongs to a
    different circuit, and everything derived from it, watts and power factor,
    inherits that. Current does not care what the meter is plugged into.
    """

    ts: datetime
    channel: int
    current: float | None
