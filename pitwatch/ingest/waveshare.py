"""Reading the panel's dry contacts from a Waveshare Ethernet I/O module.

Unlike the Shelly, this one is polled, and that is not a shortcut. It is worth
writing down why, because "surely we can just subscribe to it" is a reasonable
thing to think and the answer is no on every route the module offers.

* **The socket is already open.** This is Modbus TCP over a connection held for
  the life of the reader, not a connect per read. Keeping it open saves the
  handshake and nothing else, because Modbus is master and slave by design: a
  slave may only answer, never speak first. There is no unsolicited response in
  the specification, in RTU or in TCP.
* **MQTT does not fix it, and would make it worse.** The broker is not fixed to
  Alibaba: it is a target address and port set in VirCom, so a local Mosquitto
  works fine. But the module's MQTT mode is a transparent pipe for Modbus
  frames, not an event publisher. You publish the same `01 02 00 00 00 08` and
  it publishes the reply back. That is the same poll with a broker hop of extra
  latency and a second daemon that can die between us and an alarm.
* **The other two documented routes are also request and response.** The
  advanced applications are Alibaba MQTT, WaveshareCloud and HTTP GET/POST.
  There is no change of state upload, no active reporting and no report by
  exception anywhere in the register map or the documentation.

So it polls. The cost is about a hundred bytes a second on a LAN, and the worst
case detection delay is one poll interval, which the float debounce already
dwarfs. If a module ever does turn up that pushes, the reader is behind an
interface and this becomes the fallback.

The register map, confirmed against Waveshare's development protocol page for
the Modbus POE ETH IO 8CH:

* Function code 02, read discrete inputs, address 0, quantity 8, reads inputs 1
  through 8. Their table writes these as 0x10000 to 0x10007, which is the old
  Modbus reference notation where a leading 1 means "discrete input" and is not
  part of the address. Their own example frame settles it: `01 02 00 00 00 08`
  asks unit 1 for eight discrete inputs starting at zero.
* The default unit address is 1, and the default TCP port is 502. Both are
  settings, because both are changeable on the device.

Two pieces of processing sit between the wire and the database, and both matter
more than they look:

* **Debounce.** Float switches bounce, and they bounce for longer than most
  contacts because a float bobs. Without this, one call for water writes a
  dozen events and the run detector sees a dozen starts.
* **Inversion.** A fail safe signal reads asserted when nothing is wrong,
  whether that is a dry contact wired normally closed or a live line that
  drops on the fault. Recording the raw bit as though it were the signal would
  leave every such alarm permanently on, and silent at the moment it fires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from pitwatch.schemas import WaveshareSettings

log = logging.getLogger(__name__)

# Function code 02 starts its address space at zero. See the module docstring.
FIRST_INPUT_ADDRESS = 0
INPUT_COUNT = 8


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


class WaveshareError(Exception):
    pass


class Debouncer:
    """Holds a channel's state until the wire has agreed with itself long enough.

    One instance per reader, tracking all eight channels. A channel whose
    debounce is zero passes changes straight through, which is what you want on
    a run contact driven by a contactor auxiliary rather than by a float.
    """

    def __init__(self, settings: WaveshareSettings) -> None:
        self._hold_ms = {channel.channel: channel.debounce_ms for channel in settings.channels}
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
        that the tests do not have to sleep to exercise a timeout.
        """
        stable = self._stable.get(channel)
        if raw == stable:
            # The wire agrees with what we already believe, so any half finished
            # change was a bounce and is abandoned.
            self._candidate.pop(channel, None)
            return None

        hold = self._hold_ms.get(channel, 0) / 1000.0
        started = self._candidate.get(channel)
        if started is None or started[0] != raw:
            self._candidate[channel] = (raw, now)
            started = self._candidate[channel]

        if now - started[1] < hold:
            return None

        self._candidate.pop(channel, None)
        self._stable[channel] = raw
        return raw


class WaveshareReader:
    def __init__(
        self,
        settings: WaveshareSettings,
        on_events: Callable[[list[IoEvent]], Awaitable[None]],
        on_status: Callable[[bool, str | None], Awaitable[None]] | None = None,
        initial_state: dict[int, bool] | None = None,
    ) -> None:
        self._settings = settings
        self._on_events = on_events
        self._on_status = on_status
        self._debouncer = Debouncer(settings)
        # What the database already believes, so a restart does not replay every
        # contact as though it had just changed, and a contact that really did
        # change while the container was down is still noticed.
        self._known: dict[int, bool] = dict(initial_state or {})

    async def run(self, stop: asyncio.Event) -> None:
        delay = 1.0
        while not stop.is_set():
            client = AsyncModbusTcpClient(
                self._settings.host,
                port=self._settings.port,
                timeout=self._settings.timeout_s,
                # Reconnection is handled here, with backoff and a status
                # update, rather than silently inside the client.
                retries=1,
                reconnect_delay=0,
            )
            try:
                if not await client.connect():
                    raise WaveshareError(f"Could not connect to {self._settings.host}")
                log.info(
                    "Connected to the Waveshare at %s:%d",
                    self._settings.host,
                    self._settings.port,
                )
                await self._report(True, None)
                await self._poll_forever(client, stop)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except (WaveshareError, ModbusException, OSError, TimeoutError) as error:
                await self._report(False, str(error))
                log.warning(
                    "Waveshare at %s: %s. Retrying in %.0fs", self._settings.host, error, delay
                )
            finally:
                client.close()

            if stop.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            delay = min(delay * 2, 60.0)

    async def _poll_forever(self, client: AsyncModbusTcpClient, stop: asyncio.Event) -> None:
        interval = self._settings.poll_ms / 1000.0
        first = True
        while not stop.is_set():
            bits = await self._read_inputs(client)
            events = self._apply(bits, first)
            if events:
                await self._on_events(events)
            first = False

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

    async def _read_inputs(self, client: AsyncModbusTcpClient) -> list[bool]:
        response = await client.read_discrete_inputs(
            FIRST_INPUT_ADDRESS, count=INPUT_COUNT, device_id=self._settings.unit_id
        )
        if response.isError():
            raise WaveshareError(f"The module refused the read: {response}")
        bits = list(response.bits)[:INPUT_COUNT]
        if len(bits) < INPUT_COUNT:
            raise WaveshareError(f"Expected {INPUT_COUNT} inputs, got {len(bits)}")
        return [bool(bit) for bit in bits]

    def _apply(self, bits: list[bool], first: bool) -> list[IoEvent]:
        """Turn a frame of raw bits into the events it implies."""
        now = time.monotonic()
        stamp = datetime.now(UTC)
        events = []

        for channel_settings in self._settings.channels:
            number = channel_settings.channel
            raw = bits[number - 1]

            if first and self._debouncer.stable_state(number) is None:
                # The first frame after connecting is the truth, not a
                # transition; there is nothing to debounce it against.
                self._debouncer.prime(number, raw)
                changed = raw
            else:
                settled = self._debouncer.feed(number, raw, now)
                if settled is None:
                    continue
                changed = settled

            state = (not changed) if channel_settings.invert else changed
            if self._known.get(number) == state:
                # Same as what is already recorded. On the first frame this is
                # the normal case and writing an event would be noise; the only
                # first frame worth an event is one that differs from what was
                # true when the container stopped.
                continue
            self._known[number] = state

            events.append(
                IoEvent(
                    ts=stamp,
                    channel=number,
                    label=channel_settings.label,
                    state=state,
                    raw=raw,
                )
            )
            if channel_settings.used:
                log.info(
                    "%s went %s (DI%d reads %s)",
                    channel_settings.label,
                    "on" if state else "off",
                    number,
                    "closed" if raw else "open",
                )
        return events

    async def _report(self, online: bool, error: str | None) -> None:
        if self._on_status is not None:
            await self._on_status(online, error)


async def probe(settings: WaveshareSettings) -> dict:
    """Read the eight inputs once and report them.

    The setup page calls this on a timer while the channel map is open, so that
    someone at the panel can lift a float by hand and watch a row light up.
    That is the fastest way to get the mapping right, and getting it right by
    reading wire labels is how it ends up wrong.
    """
    client = AsyncModbusTcpClient(
        settings.host, port=settings.port, timeout=settings.timeout_s, retries=1
    )
    try:
        if not await client.connect():
            return {"ok": False, "error": f"Could not connect to {settings.host}:{settings.port}"}
        response = await client.read_discrete_inputs(
            FIRST_INPUT_ADDRESS, count=INPUT_COUNT, device_id=settings.unit_id
        )
        if response.isError():
            return {"ok": False, "error": f"The module refused the read: {response}"}

        bits = [bool(bit) for bit in list(response.bits)[:INPUT_COUNT]]
        channels = []
        for channel_settings in settings.channels:
            raw = bits[channel_settings.channel - 1]
            channels.append(
                {
                    "channel": channel_settings.channel,
                    "label": channel_settings.label,
                    "raw": raw,
                    "state": (not raw) if channel_settings.invert else raw,
                }
            )
        return {"ok": True, "channels": channels}
    except (ModbusException, OSError, TimeoutError) as error:
        return {"ok": False, "error": f"Could not reach {settings.host}: {error}"}
    finally:
        client.close()
