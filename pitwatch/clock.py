"""Times written the way the building reads them.

Storage is UTC everywhere, which is right, and every clock a person sees is
not. An alert saying 14:47 to somebody who is standing in a basement in New
York at ten to eleven in the morning is asking them to do two conversions
before they can act on it, and the conversion they are most likely to get
wrong is the one that decides whether this happened just now or overnight.

Twelve hour with AM and PM, because that is the clock this is read on. There
is a good argument for 24 hour time in a log and none at all in a text message
to a building superintendent.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# No leading zero on the hour: "9:05 AM" rather than "09:05 AM", which is how
# it would be said out loud.
TIME = "%-I:%M %p"
DATE_AND_TIME = "%-d %b " + TIME


def local(when: datetime, zone: str) -> datetime:
    """The same moment, on the site's clock.

    An unknown zone is left alone rather than raising. A settings page can hold
    a zone this machine's tzdata has never heard of, and a wrong time is a
    smaller failure than an alert that cannot be written.
    """
    try:
        return when.astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError):
        return when


def at(when: datetime, zone: str) -> str:
    """Just the time: "10:47 AM"."""
    return local(when, zone).strftime(TIME)


def on_at(when: datetime | None, zone: str) -> str:
    """The day and the time: "8 Sep 10:47 AM"."""
    if when is None:
        return ""
    return local(when, zone).strftime(DATE_AND_TIME)
