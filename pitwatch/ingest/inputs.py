"""Reading the panel's contacts from a ControlByWeb X-408 over MQTT.

The device publishes when an input changes. Nothing here asks it anything, and
there is no poll interval to tune, which is the whole point of the change: the
Modbus module this replaced was master and slave by design, a slave may never
speak first, and watching it meant asking five times a second forever and still
being up to a poll late.

**One topic carries all eight inputs.** The device is set up to publish every
input together in one JSON body whenever any of them changes, so every message
is the complete state. One message per changed input would leave this holding a
picture assembled from fragments, and a fragment lost across a reconnect would
leave that picture quietly wrong with nothing to say so.

**Online and offline come from the broker.** The device publishes a birth
message when it connects and registers a last will that the broker sends on its
behalf when it stops. So a device that loses power announces it through the
broker within the keep alive, rather than being noticed missing after a
timeout here. That is a better answer than any heartbeat: the broker knows the
socket dropped and this only has to listen.

Two pieces of processing sit between the wire and the database, and both matter
more than they look:

* **Debounce.** Float switches bounce, and they bounce for longer than most
  contacts because a float bobs. A bounce arrives as a burst of messages rather
  than a run of disagreeing polls, so a change is held until it has lasted.
* **Inversion.** A fail safe signal reads asserted when nothing is wrong,
  whether that is a dry contact wired normally closed or a live line that drops
  on the fault. The panel alarm contact on the reference panel is exactly this.
  Recording the raw bit as though it were the signal would leave every such
  alarm permanently on, and silent at the moment it fires.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import aiomqtt

from pitwatch.schemas import InputsSettings

log = logging.getLogger(__name__)

INPUT_COUNT = 8

# What the birth and last will messages say. The words are typed into the
# device as well; anything else on that topic is treated as offline, because a
# status this cannot read is not a device it can vouch for.
ONLINE = "online"

# How long to wait before reconnecting to a broker that is not there. Long
# enough not to hammer it, short enough that a broker restarting during a
# storm is not the reason nobody heard about the storm.
RECONNECT_S = 5.0


@dataclass(frozen=True, slots=True)
class IoEvent:
    """One contact changing, after debounce and after inversion."""

    ts: datetime
    channel: int
    # What the input was called when this was recorded. The channel is the
    # identity; this is here so old history still reads after a relabel.
    label: str
    state: bool
    raw: bool


class InputsError(Exception):
    pass


def parse(payload: str) -> dict[int, bool]:
    """Pull the eight input states out of one published body.

    Written to be forgiving about shape, because the body is typed into the
    device by hand and there are several reasonable ways to write it. Keys may
    be bare numbers or the device's own token names; values may be numbers,
    true and false, or on and off. What is not forgiven is a key nobody can map
    to an input, which is dropped rather than guessed at.
    """
    try:
        body = json.loads(payload)
    except (ValueError, TypeError) as error:
        raise InputsError(f"Not JSON: {payload[:80]!r}") from error

    if not isinstance(body, dict):
        raise InputsError(f"Expected an object, got {type(body).__name__}")

    states: dict[int, bool] = {}
    for key, value in _channels_in(body).items():
        state = _state_from(value)
        if state is not None:
            states[key] = state
    if not states:
        raise InputsError(f"No input states in {payload[:80]!r}")
    return states


# A key that is plainly an input: a bare number, or one of the shapes somebody
# writes when naming eight of them. The device's own token for an input is
# ${digitalInput1}, so that spelling is the likely one.
_INPUT_KEY = re.compile(r"^(?:digital[\s_-]*input|input|di|in|channel|ch)?[\s_-]*(\d+)$")


def _channels_in(body: dict) -> dict[int, object]:
    """Which entries in a body are inputs, and which input each one is.

    Two passes, and the second is the interesting one. The device offers tokens
    for things that are not inputs and some of them end in a digit:
    ``${register1}``, ``${relay1}``. Reading digits out of every key would file
    a register under input 1 and there would be nothing to notice it by. So a
    body with keys that plainly name inputs is read strictly, and the loose
    reading is kept only for a body where nothing named an input at all, which
    is what somebody who labeled the keys after the floats has written.
    """
    strict: dict[int, object] = {}
    for key, value in body.items():
        match = _INPUT_KEY.match(str(key).strip().lower())
        if match:
            number = int(match.group(1))
            if 1 <= number <= INPUT_COUNT:
                strict[number] = value
    if strict:
        return strict

    loose: dict[int, object] = {}
    for key, value in body.items():
        number = _channel_from(str(key))
        if number is not None:
            loose[number] = value
    return loose


def _channel_from(key: str) -> int | None:
    """An input number out of a key that does not name one in any usual way.

    The keys are typed into the device by hand in the payload field, so
    somebody may well have named them after the floats rather than after the
    inputs. In a body where nothing looks like an input, the digits are all
    there is to go on. Only ever reached as a fallback, for the reason in
    _channels_in.
    """
    digits = "".join(character for character in key if character.isdigit())
    if not digits:
        return None
    number = int(digits)
    return number if 1 <= number <= INPUT_COUNT else None


def _state_from(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("1", "true", "on", "yes", "closed", "high"):
            return True
        if word in ("0", "false", "off", "no", "open", "low"):
            return False
    return None


class Debouncer:
    """Holds a channel's state until it has lasted long enough to count.

    One instance per reader, tracking all eight channels against the one hold
    from the settings. Each channel keeps its own candidate, so a float
    bouncing on DI1 does not delay a contact settling on DI2.

    Different in shape from the polled version this replaces. There, a change
    was confirmed by the next poll agreeing; here nothing arrives unless
    something changes, so a candidate is confirmed by the clock rather than by
    another message. The hold is a constructor argument so the tests can set it
    to zero and drive transitions directly.
    """

    def __init__(self, hold_ms: int) -> None:
        self._hold = hold_ms / 1000.0
        self._stable: dict[int, bool] = {}
        self._candidate: dict[int, tuple[bool, float]] = {}

    def stable_state(self, channel: int) -> bool | None:
        return self._stable.get(channel)

    def prime(self, channel: int, raw: bool) -> None:
        """Set a channel's state without treating it as a change."""
        self._stable[channel] = raw
        self._candidate.pop(channel, None)

    def feed(self, channel: int, raw: bool, now: float) -> bool | None:
        """Offer a reading. Returns the new stable state, or None if unchanged.

        ``now`` is a monotonic clock reading, passed in rather than read here so
        that the tests do not have to sleep to exercise a hold.
        """
        stable = self._stable.get(channel)
        if raw == stable:
            # Back where it started, so whatever was pending was a bounce.
            self._candidate.pop(channel, None)
            return None

        started = self._candidate.get(channel)
        if started is None or started[0] != raw:
            self._candidate[channel] = (raw, now)
            started = self._candidate[channel]

        if now - started[1] < self._hold:
            return None

        self._candidate.pop(channel, None)
        self._stable[channel] = raw
        return raw

    def pending(self) -> bool:
        """Whether anything is waiting on the clock rather than on a message."""
        return bool(self._candidate)


class InputsReader:
    """Subscribes to the broker and turns published bodies into events."""

    def __init__(
        self,
        settings: InputsSettings,
        on_events: Callable[[list[IoEvent]], Awaitable[None]],
        on_status: Callable[[bool, str | None], Awaitable[None]] | None = None,
        initial_state: dict[int, bool] | None = None,
    ) -> None:
        self._settings = settings
        self._on_events = on_events
        self._on_status = on_status
        self._debouncer = Debouncer(settings.debounce_ms)
        # What the database already believes, so a restart does not replay
        # every contact as though it had just changed, and a contact that
        # really did change while this was down is still noticed.
        self._known: dict[int, bool] = dict(initial_state or {})
        self._first = True

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._listen(stop)
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, OSError, TimeoutError) as error:
                log.warning("Broker connection lost: %s", error)
                await self._report(False, str(error))
            except InputsError as error:
                # A body this cannot read is a configuration problem on the
                # device, not a reason to stop listening: the next message may
                # be fine, and the one after that is the one about the flood.
                log.error("Could not read a published body: %s", error)

            if stop.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=RECONNECT_S)

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
            log.info("Connected to the broker at %s:%d", settings.host, settings.port)
            # Subscribed before anything is said about being online, so a
            # retained message published between connecting and subscribing
            # cannot be missed.
            await client.subscribe(settings.topic)
            await client.subscribe(settings.status_topic)
            await self._report(True, None)

            async for message in client.messages:
                if stop.is_set():
                    return
                await self._handle(message)

    async def _handle(self, message: aiomqtt.Message) -> None:
        payload = message.payload
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else str(payload)
        topic = str(message.topic)

        if topic == self._settings.status_topic:
            # The device's own word for whether it is there, or the broker's
            # word on its behalf. Either way it is better than a timeout.
            online = text.strip().lower() == ONLINE
            log.info("The panel module reports %s", "online" if online else "offline")
            await self._report(online, None if online else "The module stopped talking")
            return

        events = self._apply(parse(text))
        if events:
            await self._on_events(events)

    def _apply(self, states: dict[int, bool]) -> list[IoEvent]:
        """Turn one published body into the events it implies."""
        now = asyncio.get_running_loop().time()
        stamp = datetime.now(UTC)
        events = []

        for number, raw in sorted(states.items()):
            settings = self._channel(number)

            if self._first and self._debouncer.stable_state(number) is None:
                # The first body after connecting is the truth, not a
                # transition; there is nothing to debounce it against.
                self._debouncer.prime(number, raw)
                changed = raw
            else:
                settled = self._debouncer.feed(number, raw, now)
                if settled is None:
                    continue
                changed = settled

            state = (not changed) if settings.invert else changed
            if self._known.get(number) == state:
                # Same as what is already recorded. On the first body this is
                # the normal case and writing an event would be noise; the only
                # first body worth an event is one that differs from what was
                # true when this last stopped.
                continue
            self._known[number] = state

            events.append(
                IoEvent(
                    ts=stamp,
                    channel=number,
                    label=settings.title,
                    state=state,
                    raw=raw,
                )
            )
            log.info(
                "%s went %s (DI%d reads %s)",
                settings.title,
                "on" if state else "off",
                number,
                "closed" if raw else "open",
            )

        self._first = False
        return events

    def _channel(self, number: int):
        for mapped in self._settings.channels:
            if mapped.channel == number:
                return mapped
        raise InputsError(f"No settings for input {number}")

    async def _report(self, online: bool, error: str | None) -> None:
        if self._on_status is not None:
            await self._on_status(online, error)


async def probe(settings: InputsSettings, wait_s: float = 5.0) -> dict:
    """Listen for one published body and report what it said.

    The settings page calls this while somebody is at the panel, so they can
    lift a float by hand and watch a row change. It waits rather than asks,
    because there is nothing to ask: if the device is publishing on change and
    nothing has changed, there is nothing to hear, and saying so is the honest
    answer.
    """
    try:
        async with asyncio.timeout(wait_s):
            async with aiomqtt.Client(
                hostname=settings.host,
                port=settings.port,
                username=settings.username or None,
                password=settings.password or None,
                identifier=f"{settings.client_id}-probe",
                tls_params=aiomqtt.TLSParameters() if settings.encrypted else None,
            ) as client:
                await client.subscribe(settings.topic)
                async for message in client.messages:
                    payload = message.payload
                    text = (
                        payload.decode("utf-8", "replace")
                        if isinstance(payload, bytes)
                        else str(payload)
                    )
                    states = parse(text)
                    return {
                        "ok": True,
                        "channels": [
                            {
                                "channel": mapped.channel,
                                "label": mapped.label,
                                "raw": states.get(mapped.channel),
                                "state": (
                                    None
                                    if states.get(mapped.channel) is None
                                    else (
                                        not states[mapped.channel]
                                        if mapped.invert
                                        else states[mapped.channel]
                                    )
                                ),
                            }
                            for mapped in settings.channels
                        ],
                    }
    except TimeoutError:
        return {
            "ok": False,
            # Not a failure. The connection was fine and nothing moved, which
            # is the ordinary answer from a device that speaks on change. The
            # page uses this to keep listening rather than to give up.
            "waiting": True,
            "error": (
                f"Connected to {settings.host}:{settings.port} and heard nothing on "
                f"{settings.topic} in {wait_s:.0f} seconds. The module only publishes "
                "when an input changes, so this is what you see when nothing has "
                "moved. Lift a float or push a contactor in and try again."
            ),
        }
    except (aiomqtt.MqttError, OSError) as error:
        return {"ok": False, "error": f"Could not reach the broker: {error}"}
    except InputsError as error:
        return {"ok": False, "error": str(error)}
    return {"ok": False, "error": "Nothing arrived"}
