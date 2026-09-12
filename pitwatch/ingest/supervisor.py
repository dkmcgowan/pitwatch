"""Starts the ingest tasks and restarts them when their settings change.

Changing a device address in the browser has to take effect without anyone
restarting the container, and the only honest way to apply a new address to an
open socket is to close it and open another. So each reader runs under a stop
event, and a settings change sets the event, waits for the task to finish, and
starts a fresh one from the new settings.

Settings changes that do not affect a reader are ignored. Saving an SMTP
password should not drop the connection to the meter.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import asyncpg

from pitwatch.domain.checkup import Scheduled
from pitwatch.domain.engine import AlertEngine
from pitwatch.domain.runs import RunRecorder
from pitwatch.ingest.mqtt import MqttReader
from pitwatch.ingest.sink import IoSink, LiveIo, LiveState, SampleSink, record_device_status
from pitwatch.ingest.tides import TideReader
from pitwatch.ingest.weather import WeatherReader
from pitwatch.schemas import (
    MqttSettings,
    SiteSettings,
    SummarySettings,
    TideSettings,
    WeatherSettings,
)
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

# Which settings key each reader cares about. Saving an SMTP password should
# not drop the connection to the broker, so anything not listed here is
# ignored.
MQTT_KEYS = {MqttSettings.KEY}
# Two keys, because the coordinates live on the site and the switch lives on
# the weather settings. Moving the pit and turning the rain off are both
# reasons to restart the poller.
WEATHER_KEYS = {WeatherSettings.KEY, SiteSettings.KEY}
# Two again, and for the same reason: the schedule is on the summary settings
# and the clock it runs on is the site's.
CHECKUP_KEYS = {SummarySettings.KEY, SiteSettings.KEY}
# One key. The tide needs a station rather than the site's coordinates, so
# moving the pit does not change where the water is measured.
TIDE_KEYS = {TideSettings.KEY}


class Supervisor:
    def __init__(
        self,
        pool: asyncpg.Pool,
        store: SettingsStore,
        live: LiveState,
        live_io: LiveIo,
        engine: AlertEngine | None = None,
        app=None,
    ) -> None:
        # The application, for the one task that needs the whole of it: writing
        # a health check reads the settings, the pool and the cached histories,
        # which is most of what is hung off app.state. None in the tests that
        # only exercise ingest, and the schedule is not started without it.
        self._app = app
        self._pool = pool
        self._store = store
        self._live = live
        self._live_io = live_io
        # The rules. Optional so a test can run the ingest without them, and
        # so a failure to build one cannot stop the panel being read: knowing
        # is worth having even on a day when telling is broken.
        self._engine = engine
        self.sink = SampleSink(pool, live)
        self.io_sink = IoSink(pool, live_io)

        # The live reader, so the panel's run contacts can tell it when to ask
        # the meter for a reading. None whenever nothing is configured or the
        # connection is between attempts, which is why every use is guarded.
        self._reader: MqttReader | None = None

        self._tasks: dict[str, asyncio.Task] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._watcher: asyncio.Task | None = None
        self._queue: asyncio.Queue[str] | None = None

    async def start(self) -> None:
        await self.sink.prime()
        self._spawn("sink", self.sink.run)
        if self._engine is not None:
            self._spawn("alerts", self._engine.run)
        await self._start_mqtt()
        await self._start_weather()
        await self._start_tide()
        await self._start_checkup()

        self._queue = self._store.subscribe()
        self._watcher = asyncio.create_task(self._watch_settings(), name="pitwatch-settings-watch")

    async def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
            self._watcher = None
        if self._queue is not None:
            self._store.unsubscribe(self._queue)
            self._queue = None

        # Readers first, then the sink, so that anything a reader produced on
        # its way out is written rather than dropped.
        for name in [key for key in self._tasks if key != "sink"]:
            await self._kill(name)
        await self._kill("sink")

    # -- readers ------------------------------------------------------------

    async def _start_mqtt(self) -> None:
        """One connection, carrying everything the panel has to say.

        There were two of these, and only one of them was MQTT: the meter was
        read over a websocket PitWatch opened to the device. That direction is
        what this change is about. A pull design needs a route from the
        application to every device, which works on a LAN and stops the moment
        the application is anywhere else, and it hides its own dependencies:
        the meter sat on a guest network for fifteen days with no route to the
        broker and nothing noticed, because the only path in use was the one
        the firewall happened to allow.
        """
        # Dropped first, so a restart cannot leave the run contacts talking to
        # a reader whose connection has gone.
        self._reader = None
        settings = self._store.mqtt

        async def on_status(role: str, online: bool, error: str | None) -> None:
            await record_device_status(self._pool, role, online, error)

        if not settings.enabled or not settings.host:
            log.info("Ingest is off: no broker configured")
            return
        if not (settings.used_clamps or settings.used_inputs or settings.used_health):
            log.info("Ingest has nothing to listen to: nothing is configured")
            return

        known = await self.io_sink.prime()

        # The contacts are what a run is made of, so every batch goes to the
        # recorder as well as to the event log. Recorded after, not instead: if
        # the derived layer fails, what the panel actually said is already
        # safely written down.
        recorder = RunRecorder(self._pool, self._store)

        async def on_events(events) -> None:
            await self.io_sink.submit(events)
            await recorder.record(events)
            self._watch_the_clamps()
            if self._engine is not None:
                # The moments first, then a sweep. A closing float should not
                # wait up to thirty seconds to become a message: the whole
                # argument for reading contacts rather than current was that
                # the panel says so the instant it happens.
                await self._engine.on_events(events)
                self._engine.nudge()

        reader = MqttReader(
            settings,
            on_events=on_events,
            on_samples=self.sink.submit,
            on_status=on_status,
            initial_state=known,
        )
        self._reader = reader
        self._spawn("mqtt", reader.run)
        log.info(
            "Listening to the broker at %s:%d for %d clamp(s), %d contact(s) and %d check(s)",
            settings.host,
            settings.port,
            len(settings.used_clamps),
            len(settings.used_inputs),
            len(settings.used_health),
        )

    async def _start_weather(self) -> None:
        """The rain over the pit, on a timer.

        Not a device, but the same shape as one: it runs under a stop event, it
        reports whether it is reaching anything, and it appears in
        device_status so the dashboard can say the rain is stale rather than
        quietly drawing an old forecast as a current one.
        """
        site = self._store.site
        settings = self._store.weather

        async def on_status(online: bool, error: str | None) -> None:
            await record_device_status(self._pool, "weather", online, error)

        if not settings.enabled:
            log.info("Weather is off")
            await record_device_status(self._pool, "weather", False, "Turned off")
            return
        if not site.has_coordinates:
            log.info("Weather has nowhere to look: no coordinates for the site")
            await record_device_status(self._pool, "weather", False, "No location set")
            return

        reader = WeatherReader(site, settings, self._pool, on_status)
        self._spawn("weather", reader.run)
        log.info("Weather reading for %.2f, %.2f", site.latitude, site.longitude)

    async def _start_checkup(self) -> None:
        """The scheduled health summary, if it has been switched on.

        Started whatever the settings say and stopped by them instead would be
        simpler, but a task that wakes every five minutes to decide it has
        nothing to do is a task somebody has to reason about when reading a log.
        """
        if self._app is None:
            return
        settings = self._store.summary
        if not settings.scheduled:
            log.info("The scheduled health summary is off")
            return
        if not settings.ready:
            log.info("The health summary is scheduled with nothing to ask: no key or model")
            return
        self._spawn("checkup", Scheduled(self._app).run)
        log.info(
            "The health summary is written %s at %s site time, over %s",
            settings.schedule,
            settings.schedule_at,
            settings.schedule_window,
        )

    async def _start_tide(self) -> None:
        """The water table under the pit, on a timer.

        The same shape as the weather poller and reported the same way, because
        it is the same kind of thing: not a device on the panel, but something
        outside that decides what the panel will have to do.
        """
        settings = self._store.tide

        async def on_status(online: bool, error: str | None) -> None:
            await record_device_status(self._pool, "tide", online, error)

        if not settings.enabled:
            log.info("Tides are off")
            await record_device_status(self._pool, "tide", False, "Turned off")
            return
        if not settings.station:
            log.info("Tides have nowhere to look: no station chosen")
            await record_device_status(self._pool, "tide", False, "No station chosen")
            return

        self._spawn("tide", TideReader(settings, self._pool, on_status).run)
        log.info("Tide reading from station %s", settings.station_name or settings.station)

    def _watch_the_clamps(self) -> None:
        """Tell the meter to look closely while a pump is turning.

        A meter publishes on change, which on a pit that runs for twelve
        seconds means two or three readings in the first three and nothing
        after. Measured again over MQTT on 2026-09-07 and it was the same
        shape, because it is the same notification down a different pipe. The
        panel knows when a pump started, so the reader is asked to poll for the
        length of the run rather than being left to notice.

        Read from the live contact state rather than from the events just
        handled, because a body carries every input and what matters here is
        whether either pump is running now, not which one changed.
        """
        reader = self._reader
        if reader is None:
            return
        settings = self._store.mqtt
        running = False
        for pump in (1, 2):
            channel = settings.channel_for(f"pump{pump}_run")
            if channel and self._live_io.state_of(channel):
                running = True
        reader.watch_run(running)

    # -- task plumbing ------------------------------------------------------

    def _spawn(self, name: str, coro_factory) -> None:
        stop = asyncio.Event()
        self._stops[name] = stop
        self._tasks[name] = asyncio.create_task(
            _supervised(name, coro_factory, stop), name=f"pitwatch-{name}"
        )

    async def _kill(self, name: str) -> None:
        stop = self._stops.pop(name, None)
        task = self._tasks.pop(name, None)
        if stop is not None:
            stop.set()
        if task is None:
            return
        try:
            # The reader checks its stop event between frames, so a device that
            # has gone silent will not notice for as long as its read blocks.
            # Cancel rather than wait forever.
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            log.warning("%s did not stop in time, cancelling", name)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        except asyncio.CancelledError:  # pragma: no cover -- shutdown race
            raise

    async def _watch_settings(self) -> None:
        assert self._queue is not None
        while True:
            key = await self._queue.get()
            # A save writes one key at a time but a wizard step writes several
            # in a row, so let the burst finish before reacting to it.
            await asyncio.sleep(0.5)
            keys = {key}
            while not self._queue.empty():
                keys.add(self._queue.get_nowait())

            if keys & MQTT_KEYS:
                log.info("Broker settings changed, restarting ingest")
                await self._kill("mqtt")
                await self._start_mqtt()
            if keys & TIDE_KEYS:
                log.info("Tide settings changed, restarting the poller")
                await self._kill("tide")
                await self._start_tide()
            if keys & CHECKUP_KEYS:
                log.info("Health summary settings changed, restarting the schedule")
                await self._kill("checkup")
                await self._start_checkup()
            if keys & WEATHER_KEYS:
                log.info("Weather settings changed, restarting the poller")
                await self._kill("weather")
                await self._start_weather()


async def _supervised(name: str, coro_factory, stop: asyncio.Event) -> None:
    """Run a task and make sure a crash in it is loud rather than silent.

    A bare create_task that raises puts the traceback nowhere anyone will see
    it until the process exits. This is the difference between noticing that
    ingest died and wondering why the chart stopped a week ago.
    """
    try:
        await coro_factory(stop)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ingest task %s stopped with an error", name)
        raise
    log.info("Ingest task %s stopped", name)
