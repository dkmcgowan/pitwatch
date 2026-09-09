"""The health summary, written on a clock instead of on a button.

Every day, week or thirty days, at a time on the building's own clock, over
whichever window was chosen, and optionally sent to whoever takes information
level news. Off unless somebody turned it on: something that calls out to a
model on a schedule should be a thing a person switched on rather than a thing
that started happening.

**The database decides whether one is due, not a timer.** A process that
remembers in memory forgets on every deploy, and this application is deployed
several times on a busy afternoon: a timer would have fired again each time, or
not at all. The last summary's own timestamp, read on the building's clock,
answers "has there been one this week" across restarts, across a container being
recreated, and across the clock going back in the autumn.

Counted in whole days, so a weekly one lands on the same weekday rather than
drifting an hour later each week, and a monthly one is thirty days rather than a
date that does not exist in February.

Late rather than never. One whose hour passed while the container was down runs
when the container comes back, because "last week's is the newest one you have"
is worse than one arriving at ten past nine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from pitwatch import clock
from pitwatch import summary as summaries
from pitwatch.domain import series
from pitwatch.notify import dispatch
from pitwatch.schemas import Severity
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

# How often to look at the clock. Small enough that a check lands within a few
# minutes of the hour asked for, large enough that it costs nothing: it is a
# comparison of two datetimes and, once a day, one query.
TICK_S = 300.0

# Who the schedule says wrote it. Not a user name, because no user did, and a
# page that said an account had run it at four in the morning would be wrong
# about the one thing that page is for.
BY = "the schedule"


class Scheduled:
    """Writes a health summary on a schedule, if the settings say so."""

    def __init__(self, app) -> None:
        self._app = app

    @property
    def _settings(self):
        store: SettingsStore = self._app.state.settings
        return store.summary

    async def run(self, stop: asyncio.Event) -> None:
        # Looked at once before the first sleep, not after it. One whose hour
        # passed while the container was down should not wait out another
        # interval on top of the outage, and the database is what stops it
        # running twice, so an extra look costs one query.
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                # A schedule that dies takes the summary with it silently.
                # Logged with a traceback and tried again on the next tick.
                log.exception("The scheduled health summary failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=TICK_S)

    async def tick(self, now: datetime | None = None) -> bool:
        """One look at the clock. True when a summary was written."""
        settings = self._settings
        if not (settings.scheduled and settings.ready):
            return False

        store: SettingsStore = self._app.state.settings
        zone = store.site.timezone
        now = now or datetime.now(UTC)
        here = clock.local(now, zone)
        hour, minute = settings.schedule_hour_and_minute
        if (here.hour, here.minute) < (hour, minute):
            return False

        last = await summaries.latest(self._app.state.pool)
        if last is not None:
            since = (here.date() - clock.local(last["created_at"], zone).date()).days
            if since < settings.every_days:
                return False

        window = series.window_for(settings.schedule_window, zone)
        log.info("Writing the scheduled health summary over %s", window.title)
        try:
            written = await summaries.write(self._app, BY, window)
        except summaries.SummaryError as error:
            # Not retried until the next tick, deliberately: a model that
            # refused once will refuse again in five minutes, and a schedule
            # that hammers it is worse than one that is late.
            log.warning("The scheduled health summary could not be written: %s", error)
            return False

        if settings.notify:
            await self.announce(written["body"])
        return True

    async def announce(self, body: str) -> None:
        """Send it to whoever takes information level news.

        By email and not by text. A health summary is several paragraphs of
        prose, which is several text messages, on the channel that exists here
        for two in the morning.
        """
        store: SettingsStore = self._app.state.settings
        where = store.site.where or "the pit"
        await dispatch.tell(
            self._app.state.pool,
            store,
            message=body,
            severity=Severity.INFO,
            event="written",
            subject=f"PitWatch: health summary for {where}",
            channels=("email",),
        )


__all__ = ["BY", "TICK_S", "Scheduled"]
