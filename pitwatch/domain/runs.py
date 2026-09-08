"""What the pumps actually did, recorded from the panel's own run contacts.

The clamp says how many amps flowed at 3:04:11. This says "pump 2 ran for 41
seconds at 3:04, drawing 7.2 A steady with a 46 A inrush, and it was the lag
pump". That is the layer the dashboard and the alert rules read.

**The contact decides, and the clamp describes.** A run starts when the panel
closes the run contact and ends when it opens it, and nothing about the timing
comes from current any more. It used to: runs were inferred from the load
rising off nothing, which made a count a floor rather than a tally, because two
runs close together arrive from the meter looking like one and a meter that
reports on its own schedule can miss a short run entirely. The contact has none
of those problems. It is the panel telling us, at the moment it happens, and it
is exact.

Amps keep the job they are good at. Peak, average and steady current are
recorded against the run once it closes, and they are what says a motor is on
its way out. They no longer decide whether it ran, how long for, or how many
times.

**A cycle is one call for water**: the floats rose, one or both pumps ran, the
pit emptied. A run joins the cycle that is open, and the cycle closes when its
last run does. That makes "both pumps ran on one call" answerable, which is the
question that matters on a duplex panel, because two pumps running together is
the pit winning.

The cost of that rule is at the seam: a lag pump that starts a second after the
lead pump stops is recorded as its own cycle rather than as the same call. That
is the honest reading of what the contacts said, and inventing a grace period
would be guessing at the controller's intent. Overlap is the case worth getting
right, and overlap is exactly what this gets right.
"""

from __future__ import annotations

import logging
from datetime import datetime

import asyncpg

from pitwatch.ingest.contacts import IoEvent
from pitwatch.schemas import MqttSettings

log = logging.getLogger(__name__)

# Shorter than this and it was not a pump running.
#
# The debounce in the reader already throws away electrical noise, and it is
# the right place for that: it filters on the gap between messages arriving. It
# cannot filter on what the closure meant, because at the moment an input
# closes there is no way to know how long it will stay closed. So a transient
# that holds an input for a fifth of a second passes it cleanly and arrives
# here looking like a run.
#
# It is not one. A contactor takes ten to thirty milliseconds just to pull in,
# and a run on this pit that actually moves water is twelve seconds. There is
# nothing real between those two numbers, which is what makes a floor safe.
#
# This is not paranoia about a tidy table. `both_ran` is set the instant a
# second pump joins an open cycle, before anything knows how long it will last,
# and `both_ran` is an alert: it is on the dashboard, it is a figure on the
# history page, and it sends messages. A contact that bounced for nine
# milliseconds during a normal call has already done that once on the real
# panel. The cost of a false one is somebody woken at two in the morning.
#
# A quarter of a second rather than a whole one. Half a second of HAND on the
# panel switch is a real motor start, hard on the motor and worth recording,
# and a floor high enough to swallow that would be hiding something true.
MINIMUM_RUN_S = 0.25

# The inrush is the first reading, not the first two seconds.
#
# A motor's starting surge is several times its running draw and lasts a
# moment, so a mean over the whole run describes the surge rather than the
# pump. That was excluded by time to begin with, two seconds of it, and the
# real meter showed why that does not work here: it reports on change
# rather than on a schedule, so a four second run on this pit yields one or two
# readings and a two second window throws away all of them. Every run came back
# with a null average and a null median.
#
# Dropping the first reading instead is the same intent measured in the units
# the meter actually delivers. Where a run produced only one reading it is
# kept, because one running reading is worth more than nothing and the thing it
# would otherwise be excluded for -- being the inrush -- is not true of a
# reading that arrived seconds after the start.
#
# Peak keeps every reading including the surge, deliberately: a starting
# current climbing month over month is a motor with a problem and the first
# place it shows.
#
# None of this is the main answer to "is this motor drawing more than it was".
# That is the typical load on the dashboard, a median across every running
# reading of the week, and it is sound precisely because it aggregates over
# many runs. These per run figures are a bonus and will stay thin while the
# pump runs for four seconds at a time.

RUN_STATS = """
WITH readings AS (
    SELECT current,
           row_number() OVER (ORDER BY ts) AS nth,
           count(*)     OVER ()            AS total
    FROM em_sample
    WHERE channel = $1
      AND ts >= $2::timestamptz
      AND ts <= $3::timestamptz
      AND current IS NOT NULL
)
SELECT
    max(current)                                       AS peak_current,
    min(current)                                       AS min_current,
    avg(current)      FILTER (WHERE nth > 1 OR total = 1) AS avg_current,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY current)
                      FILTER (WHERE nth > 1 OR total = 1) AS steady_current,
    count(*)                                           AS samples
FROM readings
"""


class RunRecorder:
    """Opens a pump_run when a run contact closes and completes it when it
    opens, grouping runs into cycles as it goes.

    Every write is guarded rather than assumed. A missed edge is a real
    possibility on a panel that can lose power mid run, and the failure it
    would otherwise cause is silent and lasting: a run left open forever, whose
    duration is then wrong on every page that reads it. The unique index on one
    open run per pump is the backstop, and this tries not to need it.
    """

    def __init__(self, pool: asyncpg.Pool, store) -> None:
        self._pool = pool
        self._store = store

    # -- what the settings say --------------------------------------------

    def _pump_for(self, channel: int) -> int | None:
        """Which pump's run contact this input carries, if it carries one."""
        settings: MqttSettings = self._store.mqtt
        for pump in (1, 2):
            if settings.channel_for(f"pump{pump}_run") == channel:
                return pump
        return None

    def _clamp_for(self, pump: int) -> int | None:
        settings: MqttSettings = self._store.mqtt
        return settings.clamp_for_pump.get(pump)

    # -- the edges ---------------------------------------------------------

    async def record(self, events: list[IoEvent]) -> None:
        """Take whatever the reader just confirmed and write the runs it
        implies. Called after the events themselves are stored, so a failure
        here costs the derived layer and not the record of what happened."""
        for event in sorted(events, key=lambda event: (event.ts, event.channel)):
            pump = self._pump_for(event.channel)
            if pump is None:
                continue
            try:
                if event.state:
                    await self._begin(pump, event.ts)
                else:
                    await self._finish(pump, event.ts)
            except (asyncpg.PostgresError, OSError) as error:
                log.error("Could not record pump %d %s: %s", pump, event.ts, error)

    async def _begin(self, pump: int, at: datetime) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            open_run = await connection.fetchval(
                "SELECT id FROM pump_run WHERE pump = $1 AND ended_at IS NULL", pump
            )
            if open_run is not None:
                # The panel said start twice with no stop between. Believe the
                # panel and close the orphan rather than dropping the new run:
                # the unique index would refuse it, and a pump that is running
                # now matters more than tidying up one that is not.
                log.warning("Pump %d started again with run %d still open", pump, open_run)
                await connection.execute(
                    """
                    UPDATE pump_run
                    SET ended_at = $2, duration_s = extract(epoch FROM $2 - started_at),
                        ended_by = 'timeout'
                    WHERE id = $1
                    """,
                    open_run,
                    at,
                )

            cycle_id = await connection.fetchval(
                "SELECT id FROM pump_cycle WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
            )
            if cycle_id is None:
                cycle_id = await connection.fetchval(
                    """
                    INSERT INTO pump_cycle (started_at, first_pump)
                    VALUES ($1, $2) RETURNING id
                    """,
                    at,
                    pump,
                )
                role = "lead"
            else:
                # Somebody else is already answering this call, so this is the
                # lag pump joining it. Recorded rather than worked out on read,
                # so a later correction to the inference cannot silently
                # rewrite what the dashboard said at the time.
                await connection.execute(
                    "UPDATE pump_cycle SET both_ran = true WHERE id = $1 AND first_pump <> $2",
                    cycle_id,
                    pump,
                )
                first = await connection.fetchval(
                    "SELECT first_pump FROM pump_cycle WHERE id = $1", cycle_id
                )
                role = "lead" if first == pump else "lag"

            await connection.execute(
                """
                INSERT INTO pump_run (cycle_id, pump, started_at, role, started_by)
                VALUES ($1, $2, $3, $4, 'contact')
                """,
                cycle_id,
                pump,
                at,
                role,
            )
        log.info("Pump %d started (%s)", pump, role)

    async def _finish(self, pump: int, at: datetime) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            run = await connection.fetchrow(
                """
                SELECT id, cycle_id, started_at FROM pump_run
                WHERE pump = $1 AND ended_at IS NULL
                """,
                pump,
            )
            if run is None:
                # A stop with no start. The usual cause is a restart across a
                # run, where the contact was already closed when this came up
                # and the opening edge belonged to the previous process.
                log.info("Pump %d stopped with no run open", pump)
                return

            duration = (at - run["started_at"]).total_seconds()

            # Too short to have been a pump. Thrown away here rather than left
            # in the table, because everything downstream reads this layer as
            # "what the pumps did" and a nine millisecond run is not something
            # a pump did.
            if duration < MINIMUM_RUN_S:
                await self._discard(connection, pump, run, at, duration)
                return

            stats = await self._stats(connection, pump, run["started_at"], at)

            await connection.execute(
                """
                UPDATE pump_run
                SET ended_at = $2, duration_s = $3, ended_by = 'contact',
                    peak_current = $4, avg_current = $5, steady_current = $6,
                    min_current = $7, samples = $8
                WHERE id = $1
                """,
                run["id"],
                at,
                duration,
                stats.get("peak_current"),
                stats.get("avg_current"),
                stats.get("steady_current"),
                stats.get("min_current"),
                stats.get("samples") or 0,
            )
            await self._close_cycle(connection, run["cycle_id"], at)

        log.info("Pump %d ran for %.0f s", pump, duration)

    async def _discard(self, connection, pump: int, run, at: datetime, duration: float) -> None:
        """Undo a contact closure that was too short to have been a run.

        The raw edges stay in `io_event` with their real timestamps, so nothing
        about what the panel said is lost and the blip is still there to be
        found. What gets undone is the claim built on top of it.

        Three things to put back, in this order, because each depends on the
        one before:

        1. The run itself.
        2. The cycle's `both_ran`, which was set the moment this joined an open
           cycle and could not have known better at the time. Recomputed from
           the runs that survived rather than cleared, because a cycle can
           legitimately have had both pumps for other reasons.
        3. The cycle, if this was the only run in it. A call for water that
           nothing answered was never a call for water.
        """
        log.warning(
            "Pump %d closed for %.0f ms, under the %.0f ms floor. Discarding it: "
            "a contactor takes longer than that to pull in.",
            pump,
            duration * 1000,
            MINIMUM_RUN_S * 1000,
        )
        await connection.execute("DELETE FROM pump_run WHERE id = $1", run["id"])

        cycle_id = run["cycle_id"]
        if cycle_id is None:
            return

        remaining = await connection.fetchval(
            "SELECT count(*) FROM pump_run WHERE cycle_id = $1", cycle_id
        )
        if not remaining:
            await connection.execute("DELETE FROM pump_cycle WHERE id = $1", cycle_id)
            return

        await connection.execute(
            """
            UPDATE pump_cycle
            SET both_ran = (SELECT count(DISTINCT pump) > 1 FROM pump_run WHERE cycle_id = $1)
            WHERE id = $1
            """,
            cycle_id,
        )
        # The call may have been waiting on this to finish before it could
        # close, so it still gets its chance to.
        await self._close_cycle(connection, cycle_id, at)

    async def _stats(self, connection, pump: int, started_at: datetime, ended_at: datetime) -> dict:
        """What the clamp saw while the contact was closed.

        Empty when there is no clamp for this pump or the meter said nothing,
        which is a normal way to run: the contacts alone are enough to know
        that a pump ran and for how long, and those are the two facts a
        duration is made of.
        """
        channel = self._clamp_for(pump)
        if channel is None:
            return {}
        row = await connection.fetchrow(RUN_STATS, channel, started_at, ended_at)
        return dict(row) if row else {}

    async def _close_cycle(self, connection, cycle_id: int | None, at: datetime) -> None:
        """Close the cycle once nothing in it is still running."""
        if cycle_id is None:
            return
        still_running = await connection.fetchval(
            "SELECT count(*) FROM pump_run WHERE cycle_id = $1 AND ended_at IS NULL", cycle_id
        )
        if still_running:
            return

        # Whether the pit came up past the high float at any point during the
        # call. Read off the contact's own history rather than from whatever it
        # happens to say now, because by the time the pumps have finished the
        # float has usually dropped again, which is the system working.
        high_channel = self._store.mqtt.channel_for("high_water")
        high_water = False
        if high_channel:
            high_water = bool(
                await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM io_event
                        WHERE channel = $1 AND state
                          AND ts >= (SELECT started_at FROM pump_cycle WHERE id = $2)
                          AND ts <= $3
                    )
                    """,
                    high_channel,
                    cycle_id,
                    at,
                )
            )

        await connection.execute(
            "UPDATE pump_cycle SET ended_at = $2, high_water = high_water OR $3 WHERE id = $1",
            cycle_id,
            at,
            high_water,
        )
