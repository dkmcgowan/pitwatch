"""One broker connection, and everything the panel says arriving down it.

This replaces two readers that had nothing in common but their job. The panel
contacts came over MQTT and the clamps came over a websocket that PitWatch
opened *to the meter*, and that second direction is the one that mattered:

- A pull design needs a route from the application to every device. It works on
  a LAN and stops working the moment the application is anywhere else.
- It also hides its own dependencies. The meter on the real pit sat on a guest
  SSID for fifteen days with no route to the broker and no internet, and nothing
  noticed, because the only path in use was the one direction that happened to
  be allowed through the firewall.

Every device dialing out to one broker needs one reachable address, which is
the arrangement that survives the application moving somewhere the panel cannot
see.

**Nothing in here knows what a Shelly or an X-408 is.** A source is a topic, a
profile, a path and a role, and all four are settings. See `payloads.py` for
what a profile is and why it is a shape rather than a vendor.

**Liveness is silence, not the broker's last will.** Measured on the real panel
on 2026-09-07: the meter was unplugged for twenty four seconds and its `online`
topic stayed true the whole time, then published false one hundred milliseconds
before it published true again. That is a session takeover at reconnect, not a
death notice. A will only fires once the keepalive expires, ninety seconds on
that device, and never at all for a shorter outage. So a source is judged on
whether it has said anything lately, and `expect_s` is how lately.

**Asking is for devices that go quiet while something is still happening.** A
contact module publishes when a contact moves and there is no question to put to
it in between, so nothing asks it. A meter publishes on change, which during a
steady twelve second run means two readings in the first three seconds and
silence after, so it gets asked once a second while a pump is turning. The round
trip was measured at about twenty four milliseconds against that one second, so
it costs under three percent of the interval, and only while the pit is working:
a day of runs is about four minutes of asking in twenty four hours.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import aiomqtt

from pitwatch.ingest import payloads
from pitwatch.ingest.inputs import Debouncer, IoEvent
from pitwatch.ingest.shelly import EmSample
from pitwatch.schemas import MqttSettings, MqttSource

log = logging.getLogger(__name__)

RECONNECT_S = 5.0

# How many intervals of silence before a source is called offline. The same
# reasoning as the old heartbeat check: one missed message is a dropped packet,
# and two and a half is a device that has stopped.
SILENT_MISSES = 2.5


class MqttError(Exception):
    pass


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT's own wildcard rules, because the router has to agree with the broker.

    ``+`` is exactly one level and ``#`` is the rest, including none of it. A
    subscription is made with the pattern and the broker decides what to
    deliver; this decides which source a delivered message belongs to, and the
    two answering differently would file a float reading under a clamp.
    """
    if pattern == topic:
        return True
    parts = pattern.split("/")
    actual = topic.split("/")
    for index, part in enumerate(parts):
        if part == "#":
            # Matches the remainder, and also nothing at all, so a/# matches a.
            return index <= len(actual)
        if index >= len(actual):
            return False
        if part != "+" and part != actual[index]:
            return False
    return len(parts) == len(actual)


class MqttReader:
    """Subscribes to everything configured and turns bodies into readings."""

    def __init__(
        self,
        settings: MqttSettings,
        on_events: Callable[[list[IoEvent]], Awaitable[None]],
        on_samples: Callable[[list[EmSample]], Awaitable[None]],
        on_status: Callable[[str, bool, str | None], Awaitable[None]] | None = None,
        initial_state: dict[int, bool] | None = None,
    ) -> None:
        self._settings = settings
        self._on_events = on_events
        self._on_samples = on_samples
        # Named, unlike the old one. There is no longer a fixed pair of devices
        # to report on: there are as many sources as somebody configured.
        self._on_status = on_status

        self._debouncer = Debouncer(settings.debounce_ms)
        self._known: dict[int, bool] = dict(initial_state or {})
        self._first = True
        self._nudge = asyncio.Event()

        # When each source last said anything, and whether we have already said
        # it went quiet. The flag is what stops a silent source being reported
        # offline once per check for the rest of the night.
        self._heard_at: dict[str, float] = {}
        self._silent: set[str] = set()

        # Whether a pump is turning, which is what decides whether anything is
        # being asked for a reading. Set from the panel's own run contacts by
        # the supervisor, never guessed from the current: the current is the
        # thing being measured.
        self._running = asyncio.Event()
        self._client: aiomqtt.Client | None = None

    # -- the outside world ---------------------------------------------------

    def watch_run(self, running: bool) -> None:
        """Told by the panel whether a pump is turning."""
        if running:
            self._running.set()
        else:
            self._running.clear()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._listen(stop)
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, OSError, TimeoutError) as error:
                log.warning("Broker connection lost: %s", error)
                await self._report_all(False, str(error))
            except payloads.PayloadError as error:
                # A body this cannot read is a configuration problem on the
                # device, not a reason to stop listening: the next message may
                # be fine, and the one after that is the one about the flood.
                log.error("Could not read a published body: %s", error)

            if stop.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_S)

    # -- the connection ------------------------------------------------------

    async def _listen(self, stop: asyncio.Event) -> None:
        settings = self._settings
        async with aiomqtt.Client(
            hostname=settings.host,
            port=settings.port,
            username=settings.username or None,
            password=settings.password or None,
            identifier=settings.client_id,
            tls_params=aiomqtt.TLSParameters() if settings.encrypted else None,
        ) as client:
            self._client = client
            log.info("Connected to the broker at %s:%d", settings.host, settings.port)

            # Subscribed before the clock starts, so a retained message
            # published between connecting and subscribing cannot be missed.
            for topic in self._subscriptions():
                await client.subscribe(topic)
                log.debug("Subscribed to %s", topic)

            # The clock starts now rather than at zero, so a source that is
            # perfectly healthy is given a full window to say so before
            # anything is held against it.
            began = asyncio.get_running_loop().time()
            for source in settings.used_sources:
                self._heard_at[source.role] = began
            self._silent.clear()
            await self._report_all(True, None)

            watchers = [
                asyncio.create_task(self._settle(stop)),
                asyncio.create_task(self._watch_silence(stop)),
                asyncio.create_task(self._ask_loop(stop)),
            ]
            try:
                async for message in client.messages:
                    if stop.is_set():
                        return
                    await self._handle(message)
            finally:
                self._client = None
                for watcher in watchers:
                    watcher.cancel()
                for watcher in watchers:
                    with contextlib.suppress(asyncio.CancelledError):
                        await watcher

    def _subscriptions(self) -> list[str]:
        """Every distinct topic worth listening to, subscribed once each.

        A reply topic is included, and it is often shared: several sources
        asking one device get their answers on the same topic, and subscribing
        to it twice would deliver every answer twice.
        """
        wanted: list[str] = []
        for source in self._settings.used_sources:
            for topic in (source.topic, source.answers_on if source.asks else ""):
                if topic and topic not in wanted:
                    wanted.append(topic)
        return wanted

    # -- routing -------------------------------------------------------------

    async def _handle(self, message: aiomqtt.Message) -> None:
        raw = message.payload
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        topic = str(message.topic)

        events: list[IoEvent] = []
        samples: list[EmSample] = []

        for source in self._settings.used_sources:
            # A reply is tried at its own path first, because the same body on
            # the same topic can mean different things to two sources asking
            # different questions of one device.
            if (
                source.asks
                and topic_matches(source.answers_on, topic)
                and self._collect(source, text, source.answer_path, samples, events)
            ):
                self._heard(source)
                continue
            if topic_matches(source.topic, topic):
                self._collect(source, text, source.path, samples, events)
                self._heard(source)

        if samples:
            await self._on_samples(samples)
        if events:
            await self._on_events(events)

    def _collect(
        self,
        source: MqttSource,
        text: str,
        path: str,
        samples: list[EmSample],
        events: list[IoEvent],
    ) -> bool:
        """Read one body for one source. True when it carried anything."""
        try:
            if source.role in ("clamp1", "clamp2"):
                reading = payloads.value(source.profile, text, path)
                if reading is None:
                    return False
                samples.append(self._sample(source, reading))
                return True
            if source.role == "contacts":
                states = payloads.contact_states(source.profile, text, path)
                if source.input_number and source.profile == "contact":
                    # One contact on its own topic, renumbered to the input it
                    # is wired to. The parser cannot know that; the setting
                    # does. There is exactly one state in there, because that
                    # is what the profile means.
                    only = next(iter(states.values()), None)
                    states = {} if only is None else {source.input_number: only}
                events.extend(self._apply(states))
                return bool(states)
            if source.role == "heartbeat":
                # Proof of life by arriving, not by what it says. A module's
                # own heartbeat body is an id and an uptime with no status
                # field in it at all, and reading it for one would mark a
                # healthy module dead every sixty seconds.
                return True
        except payloads.PayloadError as error:
            log.warning("%s: %s", source.title, error)
        return False

    def _sample(self, source: MqttSource, reading: float) -> EmSample:
        """A reading, filed under the channel this source records against.

        The channel is a setting rather than the role's number, because the
        readings already stored are filed under whatever the meter called its
        clamps. Changing that would leave last month's amps describing the
        other pump.
        """
        channel = source.channel if source.channel is not None else 0
        return EmSample(
            ts=datetime.now(UTC),
            channel=channel,
            current=reading,
            voltage=None,
            act_power=None,
            aprt_power=None,
            pf=None,
            freq=None,
        )

    def _heard(self, source: MqttSource) -> None:
        self._heard_at[source.role] = asyncio.get_running_loop().time()
        if source.role in self._silent:
            self._silent.discard(source.role)
            log.info("%s is talking again", source.title)

    # -- contacts ------------------------------------------------------------

    def _apply(self, states: dict[int, bool]) -> list[IoEvent]:
        """Turn one published body into the events it implies."""
        now = asyncio.get_running_loop().time()
        confirmed: dict[int, bool] = {}

        for number, raw in sorted(states.items()):
            if self._channel(number) is None:
                continue
            if self._first and self._debouncer.stable_state(number) is None:
                # The first body after connecting is the truth, not a
                # transition; there is nothing to debounce it against.
                self._debouncer.prime(number, raw)
                confirmed[number] = raw
            else:
                settled = self._debouncer.feed(number, raw, now)
                if settled is not None:
                    confirmed[number] = settled

        self._first = False
        if self._debouncer.next_deadline(now) is not None:
            self._nudge.set()
        return self._events_for(confirmed)

    def _events_for(self, confirmed: dict[int, bool]) -> list[IoEvent]:
        """Settled raw readings, inverted where the signal is fail safe, and
        written down only where they differ from what is already recorded."""
        stamp = datetime.now(UTC)
        events = []

        for number, changed in sorted(confirmed.items()):
            settings = self._channel(number)
            if settings is None:
                continue

            state = (not changed) if settings.invert else changed
            if self._known.get(number) == state:
                continue
            self._known[number] = state

            events.append(
                IoEvent(
                    ts=stamp,
                    channel=number,
                    label=settings.title,
                    state=state,
                    raw=changed,
                )
            )
            log.info(
                "%s went %s (input %d reads %s)",
                settings.title,
                "on" if state else "off",
                number,
                "closed" if changed else "open",
            )
        return events

    def _channel(self, number: int):
        for mapped in self._settings.channels:
            if mapped.channel == number:
                return mapped
        return None

    async def _settle(self, stop: asyncio.Event) -> None:
        """Confirm changes that the clock has settled.

        A source that speaks only when something changes cannot confirm a
        change with a second message, because there is no second message: a
        high water float closes once and stays closed. So the hold elapsing is
        what confirms it, and this is what watches the clock.
        """
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            self._nudge.clear()
            delay = self._debouncer.next_deadline(loop.time())

            if delay is None:
                await self._nudge.wait()
                continue
            if delay > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._nudge.wait(), timeout=delay)
                continue

            events = self._events_for(self._debouncer.settled(loop.time()))
            if events:
                await self._on_events(events)

    # -- liveness ------------------------------------------------------------

    async def _watch_silence(self, stop: asyncio.Event) -> None:
        """Hold every source to its own interval.

        Opt in per source. At zero this does nothing for that source, which is
        right for one whose device was never asked to speak on a schedule:
        holding silence against it would paint a permanent red and teach
        somebody to ignore the indicator.
        """
        expecting = [s for s in self._settings.used_sources if s.expect_s]
        if not expecting:
            return

        loop = asyncio.get_running_loop()
        tick = max(1.0, min(source.expect_s for source in expecting) / 2)
        while not stop.is_set():
            # Checked several times per window rather than once, so the news is
            # at most a fraction of an interval late.
            await asyncio.sleep(tick)
            for source in expecting:
                if source.role in self._silent:
                    continue
                last = self._heard_at.get(source.role)
                if last is None:
                    continue
                silent = loop.time() - last
                if silent < source.expect_s * SILENT_MISSES:
                    continue
                self._silent.add(source.role)
                log.warning("Nothing from %s for %.0f s", source.title, silent)
                await self._report(
                    source,
                    False,
                    f"Nothing heard for {silent:.0f} s, expected every {source.expect_s} s",
                )

    # -- asking --------------------------------------------------------------

    async def _ask_loop(self, stop: asyncio.Event) -> None:
        """Ask the sources that have something to be asked.

        Only while a pump is turning, where the source says so. The panel's own
        run contact is what decides that, so a pit sitting still is a pit
        nothing is polling.
        """
        asking = [source for source in self._settings.used_sources if source.asks]
        if not asking:
            return

        while not stop.is_set():
            waiting = [source for source in asking if source.ask_while_running]
            if waiting and not self._running.is_set():
                # Nothing to do until the panel says a pump started. The tail
                # is for the decay: a motor coasting down draws less than it
                # did and the contact has already opened.
                done, _ = await asyncio.wait(
                    [
                        asyncio.create_task(self._running.wait()),
                        asyncio.create_task(stop.wait()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task.cancel()
                if stop.is_set():
                    return

            for source in asking:
                if source.ask_while_running and not self._running.is_set():
                    continue
                await self._ask(source)

            delay = min(source.ask_every_s for source in asking)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _ask(self, source: MqttSource) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.publish(source.ask_topic, source.ask_payload)
        except (aiomqtt.MqttError, OSError) as error:
            log.warning("Could not ask %s: %s", source.title, error)

    # -- saying how it went --------------------------------------------------

    async def _report(self, source: MqttSource, online: bool, error: str | None) -> None:
        if self._on_status is not None:
            await self._on_status(source.role, online, error)

    async def _report_all(self, online: bool, error: str | None) -> None:
        for source in self._settings.used_sources:
            await self._report(source, online, error)


__all__ = ["MqttError", "MqttReader", "topic_matches"]
