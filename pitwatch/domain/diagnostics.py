"""What has actually arrived, per source, for the person who wired it.

The dashboard is read by somebody who wants to know whether the pit is
alright. It answers in one vocabulary on purpose: an overload that has not
tripped and an overload nothing has ever been heard from both read "never" and
"0 this month", because to that reader they mean the same thing, which is that
nothing has happened.

The difference between those two is real and it belongs here instead. Somebody
looking at this page has just wired a panel or is chasing a topic that does not
match, and for them "nothing has ever arrived on this topic" is the whole
answer. It is a page for that hour, not for every hour after it.

Nothing here is derived cleverly. It is the last message per input, the
readings per clamp, and the health rows, put beside the settings that were
supposed to produce them, so that a row with a topic and no traffic is visible
as exactly that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import asyncpg

from pitwatch import clock
from pitwatch.schemas import MqttSettings, PumpsSettings, SiteSettings

# A day is the window for "is this wired up", because the question is being
# asked while somebody is standing at the panel. Anything older than that is
# answered by the history page.
#
# A timedelta rather than the string "24 hours": asyncpg binds an interval
# parameter from a timedelta and refuses a str, and the failure is at query
# time rather than at import, so it reaches the page as a red line.
WINDOW = timedelta(hours=24)

INPUTS = """
SELECT s.channel,
       s.state,
       s.raw,
       s.changed_at,
       s.updated_at,
       coalesce(e.events, 0) AS events
FROM io_state s
LEFT JOIN (
    SELECT channel, count(*) AS events
    FROM io_event
    WHERE ts > now() - $1::interval
    GROUP BY channel
) e ON e.channel = s.channel
"""

CLAMPS = """
SELECT channel,
       count(*)      AS readings,
       max(ts)       AS last_reading,
       max(current)  AS peak,
       avg(current)  AS mean
FROM em_sample
WHERE ts > now() - $1::interval
GROUP BY channel
"""

HEALTH = "SELECT device, online, last_seen, last_error FROM device_status"


@dataclass
class Row:
    """One source, what it was told to listen to, and what turned up."""

    name: str
    carries: str
    topic: str
    heard: bool
    detail: str
    # Already written on the site's clock, because a page that hands a UTC
    # timestamp to somebody standing at a panel is asking them to do the sum.
    last: str = "never"
    note: str = ""


@dataclass
class Report:
    inputs: list[Row] = field(default_factory=list)
    clamps: list[Row] = field(default_factory=list)
    health: list[Row] = field(default_factory=list)
    watchouts: list[str] = field(default_factory=list)

    @property
    def silent(self) -> int:
        """Configured sources nothing has arrived on. The number worth acting on."""
        return sum(1 for row in self.rows if row.topic and not row.heard)

    @property
    def rows(self) -> list[Row]:
        return [*self.inputs, *self.clamps, *self.health]


def _amps(value: float | None) -> str:
    return "--" if value is None else f"{value:.2f} A"


async def read(
    pool: asyncpg.Pool, mqtt: MqttSettings, pumps: PumpsSettings, site: SiteSettings
) -> Report:
    """Every configured source, beside what has arrived on it."""
    report = Report()
    if not mqtt.enabled or not mqtt.host:
        report.watchouts.append("MQTT is switched off, so nothing is being listened for.")
        return report

    try:
        input_rows = {row["channel"]: row for row in await pool.fetch(INPUTS, WINDOW)}
        clamp_rows = {row["channel"]: row for row in await pool.fetch(CLAMPS, WINDOW)}
        health_rows = {row["device"]: row for row in await pool.fetch(HEALTH)}
    except (asyncpg.PostgresError, OSError) as error:  # pragma: no cover -- reported, not raised
        report.watchouts.append(f"Could not read what has arrived: {error}")
        return report

    for contact in mqtt.inputs:
        if not contact.used:
            continue
        seen = input_rows.get(contact.channel)
        heard = seen is not None
        if heard:
            now = "closed" if seen["state"] else "open"
            detail = f"{now}, {seen['events']} change(s) in the last day"
        else:
            detail = "nothing has arrived"
        report.inputs.append(
            Row(
                name=f"Input {contact.channel}",
                carries=contact.title,
                topic=contact.topic,
                heard=heard,
                detail=detail,
                last=clock.on_at(seen["updated_at"], site.timezone) if heard else "never",
                note="" if heard else "Check the topic.",
            )
        )

    for clamp in mqtt.clamps:
        if not clamp.configured:
            continue
        seen = clamp_rows.get(clamp.channel)
        heard = seen is not None and seen["readings"] > 0
        if heard:
            detail = f"{seen['readings']} readings in the last day, peak {_amps(seen['peak'])}"
        else:
            detail = "nothing has arrived"
        # A clamp reading a flat zero all day is the shape of a CT that is not
        # around a wire, and it is indistinguishable from a pump that has not
        # run except by the run contact beside it.
        note = ""
        if heard and (seen["peak"] or 0) == 0:
            note = "Every reading is zero, which is what an unfitted clamp looks like."
        elif not heard:
            note = "Check the topic and the ask."
        report.clamps.append(
            Row(
                name=pumps.by_number[clamp.pump].name or f"Pump {clamp.pump}",
                carries="Current",
                topic=clamp.topic,
                heard=heard,
                detail=detail,
                last=clock.on_at(seen["last_reading"], site.timezone) if heard else "never",
                note=note,
            )
        )

    for index, check in enumerate(mqtt.health):
        if not check.configured:
            continue
        seen = health_rows.get(f"health{index}")
        # A row exists for every health check from the moment the database is
        # created, so the row is not the evidence. last_seen is: null means
        # nothing has ever arrived on this topic, which is the answer this page
        # exists to give.
        heard = bool(seen and seen["last_seen"])
        online = bool(seen and seen["online"])
        detail = (
            "up" if online else (seen["last_error"] if seen and seen["last_error"] else "quiet")
        )
        # The fault that cost a day of false alarms: a health topic that only
        # publishes when something changes has no floor, so any interval short
        # enough to catch a real outage is short enough to trip on a quiet
        # patch. There is no way to tell those apart from here, so it says so
        # rather than guessing.
        report.health.append(
            Row(
                name=check.name or f"Device {index + 1}",
                carries=f"Expected every {check.expect_s} s",
                topic=check.topic,
                heard=heard,
                detail=detail if heard else "nothing has arrived",
                last=clock.on_at(seen["last_seen"], site.timezone) if heard else "never",
                note="" if online else "Use a topic the device sends on a schedule.",
            )
        )

    silent = [row for row in report.rows if row.topic and not row.heard]
    if silent:
        report.watchouts.append(
            "Nothing has arrived on: " + ", ".join(row.name for row in silent) + "."
        )
    for row in report.rows:
        if row.note and row.heard:
            report.watchouts.append(f"{row.name}: {row.note}")
    if not report.watchouts:
        report.watchouts.append("Every configured source has been heard from in the last day.")
    return report
