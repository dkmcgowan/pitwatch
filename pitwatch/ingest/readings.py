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
    """One reading from one clamp.

    ``channel`` is which recorded channel this belongs under, which is a
    setting on the source rather than anything the device knows: nothing on a
    meter knows which motor a clamp is around. Readings already stored are
    filed under those numbers, so they are carried rather than derived.
    """

    ts: datetime
    channel: int
    current: float | None
    voltage: float | None
    act_power: float | None
    aprt_power: float | None
    pf: float | None
    freq: float | None
