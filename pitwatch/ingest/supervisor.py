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

from pitwatch.ingest.inputs import InputsReader
from pitwatch.ingest.shelly import ShellyReader
from pitwatch.ingest.sink import IoSink, LiveIo, LiveState, SampleSink, record_device_status
from pitwatch.schemas import InputsSettings, ShellySettings
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

# Which settings key each reader cares about. Saving an SMTP password should
# not drop the connection to the meter, so anything not listed here is ignored.
SHELLY_KEYS = {ShellySettings.KEY}
INPUT_KEYS = {InputsSettings.KEY}


class Supervisor:
    def __init__(
        self, pool: asyncpg.Pool, store: SettingsStore, live: LiveState, live_io: LiveIo
    ) -> None:
        self._pool = pool
        self._store = store
        self._live = live
        self._live_io = live_io
        self.sink = SampleSink(pool, live)
        self.io_sink = IoSink(pool, live_io)

        self._tasks: dict[str, asyncio.Task] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._watcher: asyncio.Task | None = None
        self._queue: asyncio.Queue[str] | None = None

    async def start(self) -> None:
        await self.sink.prime()
        self._spawn("sink", self.sink.run)
        await self._start_shelly()
        await self._start_inputs()

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

    async def _start_shelly(self) -> None:
        settings = self._store.shelly
        if not settings.enabled or not settings.host:
            log.info("Shelly ingest is off: no address configured")
            await record_device_status(self._pool, "shelly", False, "Not configured")
            return

        async def on_status(online: bool, error: str | None) -> None:
            await record_device_status(self._pool, "shelly", online, error)

        reader = ShellyReader(settings, self.sink.submit, on_status)
        self._spawn("shelly", reader.run)
        log.info("Shelly ingest started for %s", settings.host)

    async def _start_inputs(self) -> None:
        settings = self._store.inputs
        if not settings.enabled or not settings.host:
            log.info("Panel input ingest is off")
            await record_device_status(self._pool, "inputs", False, "Not configured")
            return

        async def on_status(online: bool, error: str | None) -> None:
            await record_device_status(self._pool, "inputs", online, error)

        known = await self.io_sink.prime()
        reader = InputsReader(settings, self.io_sink.submit, on_status, initial_state=known)
        self._spawn("inputs", reader.run)
        log.info(
            "Panel input ingest listening to the broker at %s:%d", settings.host, settings.port
        )

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

            if keys & SHELLY_KEYS:
                log.info("Shelly settings changed, restarting ingest")
                await self._kill("shelly")
                await self._start_shelly()
            if keys & INPUT_KEYS:
                log.info("Panel input settings changed, restarting ingest")
                await self._kill("inputs")
                await self._start_inputs()


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
