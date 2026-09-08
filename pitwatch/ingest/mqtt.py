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

**Nothing in here knows what any particular device is.** Three kinds of thing
to listen to, because a pump panel asks three kinds of question: a clamp is a
number at a topic, a contact is on or off at a topic, and a health check is a
topic that ought to say something now and then. Each is a row somebody fills
in.

One contact per topic rather than several in one body, which costs a parser
that has to guess how somebody spelled eight keys.

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
from pitwatch.ingest.contacts import Debouncer, IoEvent
from pitwatch.ingest.readings import EmSample
from pitwatch.schemas import ClampSource, ContactInput, MqttSettings

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
            for key in self._reported():
                self._heard_at[key] = began
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

        A reply topic can be shared: two clamps asking one meter may get their
        answers on one topic as long as they read different paths out of it,
        and subscribing to it twice would deliver every answer twice.
        """
        wanted: list[str] = []
        for clamp in self._settings.used_clamps:
            for topic in (clamp.topic, clamp.answers_on if clamp.asks else ""):
                if topic and topic not in wanted:
                    wanted.append(topic)
        for one in self._settings.used_inputs:
            if one.topic not in wanted:
                wanted.append(one.topic)
        for check in self._settings.used_health:
            if check.topic not in wanted:
                wanted.append(check.topic)
        return wanted

    # -- routing -------------------------------------------------------------

    async def _handle(self, message: aiomqtt.Message) -> None:
        raw = message.payload
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        topic = str(message.topic)

        samples: list[EmSample] = []
        states: dict[int, bool] = {}

        for clamp in self._settings.used_clamps:
            # A reply is tried at its own path first, because the same body on
            # the same topic can mean different things to two clamps asking
            # different questions of one meter.
            if clamp.asks and topic_matches(clamp.answers_on, topic):
                reading = payloads.value(text, clamp.answer_path)
                if reading is not None:
                    samples.append(self._sample(clamp, reading))
                    self._heard(clamp.role, clamp.role)
                    continue
            if topic_matches(clamp.topic, topic):
                reading = payloads.value(text, clamp.path)
                if reading is not None:
                    samples.append(self._sample(clamp, reading))
                self._heard(clamp.role, clamp.role)

        for one in self._settings.used_inputs:
            if not topic_matches(one.topic, topic):
                continue
            said = payloads.state(text, one.path)
            if said is None:
                # Nothing for this input in this body. Where a path is set that
                # is ordinary: several inputs may share a topic and each read
                # its own key out of it, so most bodies are not about most
                # inputs. Where no path is set the body was meant to be the
                # state itself, and something that is neither on nor off is
                # worth hearing about.
                if not one.path:
                    log.warning("%s published %r, which is not on or off", one.title, text[:60])
                continue
            states[one.channel] = said
            self._heard(f"input{one.channel}", one.title)

        for index, check in enumerate(self._settings.health):
            # Proof of life by arriving, not by what it says. A module's own
            # heartbeat body is an id and an uptime with no status field in it
            # at all, and reading it for one would mark a healthy module dead
            # every sixty seconds.
            if check.configured and topic_matches(check.topic, topic):
                self._heard(f"health{index}", check.title)

        if samples:
            await self._on_samples(samples)
        if states:
            events = self._apply(states)
            if events:
                await self._on_events(events)

    def _sample(self, clamp: ClampSource, reading: float) -> EmSample:
        """A reading, filed under the channel this clamp records against.

        Derived from the pump number rather than configured, because there is
        no other sensible answer.
        """
        return EmSample(
            ts=datetime.now(UTC),
            channel=clamp.channel,
            current=reading,
        )

    def _heard(self, key: str, title: str) -> None:
        self._heard_at[key] = asyncio.get_running_loop().time()
        if key in self._silent:
            self._silent.discard(key)
            log.info("%s is talking again", title)

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

    def _channel(self, number: int) -> ContactInput | None:
        return self._settings.input_at(number)

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
        """Hold every health check to its own interval.

        Opt in per check. At zero this does nothing for that one, which is
        right for a device that was never asked to speak on a schedule:
        holding silence against it would paint a permanent red and teach
        somebody to ignore the indicator.
        """
        expecting = [
            (index, check)
            for index, check in enumerate(self._settings.health)
            if check.configured and check.expect_s
        ]
        if not expecting:
            return

        loop = asyncio.get_running_loop()
        tick = max(1.0, min(check.expect_s for _, check in expecting) / 2)
        while not stop.is_set():
            # Checked several times per window rather than once, so the news is
            # at most a fraction of an interval late.
            await asyncio.sleep(tick)
            for index, check in expecting:
                key = f"health{index}"
                if key in self._silent:
                    continue
                last = self._heard_at.get(key)
                if last is None:
                    continue
                silent = loop.time() - last
                if silent < check.expect_s * SILENT_MISSES:
                    continue
                self._silent.add(key)
                log.warning("Nothing from %s for %.0f s", check.title, silent)
                await self._report(
                    key,
                    False,
                    f"Nothing heard for {silent:.0f} s, expected every {check.expect_s} s",
                )

    # -- asking --------------------------------------------------------------

    async def _ask_loop(self, stop: asyncio.Event) -> None:
        """Ask the clamps that have something to be asked.

        Only while a pump is turning. The panel's own run contact decides that,
        so a pit sitting still is a pit nothing is polling: a day of runs is
        about four minutes of asking in twenty four hours.
        """
        asking = [clamp for clamp in self._settings.used_clamps if clamp.asks]
        if not asking:
            return

        while not stop.is_set():
            if not self._running.is_set():
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

            for clamp in asking:
                await self._ask(clamp)

            delay = min(clamp.ask_every_s for clamp in asking)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _ask(self, clamp: ClampSource) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.publish(clamp.ask_topic, clamp.ask_payload)
        except (aiomqtt.MqttError, OSError) as error:
            log.warning("Could not ask the pump %d clamp: %s", clamp.pump, error)

    # -- saying how it went --------------------------------------------------

    async def _report(self, key: str, online: bool, error: str | None) -> None:
        if self._on_status is not None:
            await self._on_status(key, online, error)

    def _reported(self) -> list[str]:
        """What appears in device_status: the clamps and the health checks.

        Not the contacts. Eight inputs would be eight rows saying the same
        thing about one module, which is what a health check is for.
        """
        keys = [clamp.role for clamp in self._settings.used_clamps]
        keys += [
            f"health{index}"
            for index, check in enumerate(self._settings.health)
            if check.configured
        ]
        return keys

    async def _report_all(self, online: bool, error: str | None) -> None:
        for key in self._reported():
            await self._report(key, online, error)


__all__ = ["MqttError", "MqttReader", "topic_matches"]
