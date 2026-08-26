"""Talking to a Shelly EM Gen3 over its local RPC websocket.

The device pushes. PitWatch opens a socket to ``ws://<addr>/rpc``, sends one
request so the device knows who to send notifications to, and then reads
``NotifyStatus`` frames as the readings change. There is no polling loop in the
normal path; the only scheduled request is a heartbeat whose job is to notice
that the device has gone quiet, which a socket that is merely idle looks
exactly like.

Two things about the protocol are worth knowing before reading the code:

* Notifications are gated. The device sends nothing until the client has sent
  at least one request frame carrying a ``src``, and it then addresses
  notifications to that ``src``. Connecting and waiting produces silence.
* Authentication, when the device has a password, is digest over the same
  socket: the first request comes back as a 401 error carrying a nonce, and the
  request is sent again with an ``auth`` object. On the websocket channel the
  HA2 half of the digest is the literal string "dummy_method:dummy_uri", which
  looks like a placeholder somebody forgot to replace and is in fact the
  specified value.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx2
import websockets
from websockets.exceptions import (
    InvalidProxy,
    InvalidProxyStatus,
    InvalidStatus,
    ProxyError,
    WebSocketException,
)

from pitwatch.schemas import ShellySettings

log = logging.getLogger(__name__)

# The device puts this in the dst field of everything it sends us.
CLIENT_ID = "pitwatch"

# Shelly's local account has exactly one user name and it is not configurable.
AUTH_USERNAME = "admin"

CONNECT_TIMEOUT_S = 10
REQUEST_TIMEOUT_S = 10


@dataclass(frozen=True, slots=True)
class EmSample:
    """One reading from one clamp.

    ``channel`` is the em1 instance the device reported, 0 or 1. Mapping that to
    a pump happens later, from settings, because nothing on the device knows
    which motor a clamp is around.
    """

    ts: datetime
    channel: int
    current: float | None
    voltage: float | None
    act_power: float | None
    aprt_power: float | None
    pf: float | None
    freq: float | None

    @classmethod
    def from_status(cls, channel: int, status: dict, ts: datetime) -> EmSample:
        def number(key: str) -> float | None:
            value = status.get(key)
            return float(value) if isinstance(value, int | float) else None

        return cls(
            ts=ts,
            channel=channel,
            current=number("current"),
            voltage=number("voltage"),
            act_power=number("act_power"),
            aprt_power=number("aprt_power"),
            pf=number("pf"),
            freq=number("freq"),
        )


class ShellyError(Exception):
    """Anything the device refused to do."""


class ShellyAuthError(ShellyError):
    """The device wants a password, or did not like the one it was given."""


def digest_response(challenge: dict, password: str, cnonce: str, nc: int) -> dict:
    """Build the auth object for a digest challenge on the websocket channel."""
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")

    def sha256(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    ha1 = sha256(f"{AUTH_USERNAME}:{realm}:{password}")
    # Specified as a literal on this channel. See the module docstring.
    ha2 = sha256("dummy_method:dummy_uri")
    nc_hex = f"{nc:08x}"
    response = sha256(f"{ha1}:{nonce}:{nc_hex}:{cnonce}:auth:{ha2}")

    return {
        "realm": realm,
        "username": AUTH_USERNAME,
        "nonce": nonce,
        "cnonce": cnonce,
        "nc": nc_hex,
        "response": response,
        "algorithm": "SHA-256",
    }


def parse_notify_status(frame: dict) -> list[EmSample]:
    """Pull clamp readings out of a NotifyStatus or NotifyFullStatus frame.

    A delta frame carries only what changed, so a frame with a voltage and no
    current is normal and produces a sample with a null current rather than
    being dropped. Filling the gap from the last known value would invent
    readings, and this is a meter.
    """
    params = frame.get("params") or {}
    # The device timestamps its own notifications in Unix seconds. Trusting it
    # keeps the readings in the order the device saw them even when a burst
    # arrives together, and the clock check in ShellyReader catches a device
    # whose time is wrong.
    reported = params.get("ts")
    ts = (
        datetime.fromtimestamp(reported, tz=UTC)
        if isinstance(reported, int | float)
        else datetime.now(UTC)
    )

    samples = []
    for key, value in params.items():
        if not key.startswith("em1:") or not isinstance(value, dict):
            continue
        try:
            channel = int(key.split(":", 1)[1])
        except ValueError:  # pragma: no cover -- the device does not do this
            log.debug("Ignoring a component key that is not numbered: %r", key)
            continue
        samples.append(EmSample.from_status(channel, value, ts))
    return samples


class ShellyConnection:
    """One open websocket to one device, with request and response matching."""

    def __init__(self, settings: ShellySettings) -> None:
        self._settings = settings
        self._socket: websockets.ClientConnection | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._nonce_count = 0
        # Notifications land here so that reading the socket and consuming
        # notifications are not the same loop. See _read_forever.
        self._notifications: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=256)
        self._reader: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"ws://{self._settings.host}/rpc"

    async def open(self) -> None:
        self._socket = await asyncio.wait_for(
            websockets.connect(
                self.url,
                open_timeout=CONNECT_TIMEOUT_S,
                max_queue=64,
                # Never through a proxy. The websockets library defaults to
                # proxy=True, which reads HTTP_PROXY and ALL_PROXY from the
                # environment, and a meter on the LAN is the last thing that
                # should be reached through one. Worse, it fails in a way that
                # looks like the device: plain HTTP to the same device works,
                # because a proxy will forward that happily, while the upgrade
                # needs a CONNECT tunnel the proxy may well refuse.
                proxy=None,
            ),
            timeout=CONNECT_TIMEOUT_S,
        )
        self._reader = asyncio.create_task(
            self._read_forever(), name=f"shelly-read-{self._settings.host}"
        )
        log.info("Connected to the Shelly at %s", self._settings.host)

    async def close(self) -> None:
        if self._socket is not None:
            with contextlib.suppress(WebSocketException, OSError):
                await self._socket.close()
            self._socket = None
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        self._fail_pending(ShellyError("The connection was closed"))
        # Unblock anyone iterating frames(), which is otherwise waiting on a
        # queue nothing will ever put anything on again.
        with contextlib.suppress(asyncio.QueueFull):
            self._notifications.put_nowait(None)

    async def request(self, method: str, params: dict | None = None) -> dict:
        """Send an RPC request and wait for its answer, retrying once with auth.

        The retry is not a loop. A second 401 means the password is wrong, and
        hammering a device with a bad password is how you end up locked out of
        it.
        """
        result = await self._send(method, params)
        if "error" not in result:
            return result.get("result") or {}

        error = result["error"]
        if error.get("code") != 401:
            raise ShellyError(f"{method} failed: {error.get('message', error)}")

        if not self._settings.password:
            raise ShellyAuthError(
                "The Shelly is asking for a password. Add it in the settings page."
            )

        challenge = _challenge_from(error)
        self._nonce_count += 1
        auth = digest_response(
            challenge, self._settings.password, secrets.token_hex(8), self._nonce_count
        )
        result = await self._send(method, params, auth=auth)
        if "error" in result:
            raise ShellyAuthError("The Shelly rejected the password")
        return result.get("result") or {}

    async def _send(self, method: str, params: dict | None, auth: dict | None = None) -> dict:
        if self._socket is None:
            raise ShellyError("Not connected")

        self._next_id += 1
        request_id = self._next_id
        frame: dict = {"id": request_id, "src": CLIENT_ID, "method": method}
        if params is not None:
            frame["params"] = params
        if auth is not None:
            frame["auth"] = auth

        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._socket.send(json.dumps(frame))
            return await asyncio.wait_for(future, timeout=REQUEST_TIMEOUT_S)
        except TimeoutError as error:
            raise ShellyError(f"{method} timed out") from error
        finally:
            self._pending.pop(request_id, None)

    async def _read_forever(self) -> None:
        """Drain the socket for as long as it is open.

        This runs as its own task from the moment the connection opens, and it
        is the only thing that ever reads the socket. That matters more than it
        looks: a reply only arrives because something is reading, so if this
        were folded into the notification loop instead, the first request would
        wait on a future that nothing could possibly resolve until somebody
        started iterating notifications. Which is after the first request. The
        result is a socket that connects perfectly and then times out on every
        call.
        """
        assert self._socket is not None
        try:
            async for message in self._socket:
                try:
                    frame = json.loads(message)
                except json.JSONDecodeError:
                    log.warning("Ignoring a frame that is not JSON")
                    continue
                if not isinstance(frame, dict):
                    continue

                future = self._pending.get(frame.get("id"))
                if future is not None:
                    if not future.done():
                        future.set_result(frame)
                    continue
                if frame.get("method"):
                    await self._notifications.put(frame)
        except (WebSocketException, OSError) as error:
            log.debug("Shelly read loop ended: %s", error)
        finally:
            # Whoever is waiting needs to hear that nothing more is coming,
            # rather than sitting out the full request timeout for an answer
            # that cannot arrive.
            self._fail_pending(ShellyError("The connection closed"))
            await self._notifications.put(None)

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def frames(self):
        """Yield notification frames until the connection closes."""
        while True:
            frame = await self._notifications.get()
            if frame is None:
                return
            yield frame


def _challenge_from(error: dict) -> dict:
    """Dig the digest challenge out of a 401 error frame.

    Firmware has shipped it both as a JSON string in ``message`` and as an
    object, so both are accepted.
    """
    message = error.get("message")
    if isinstance(message, dict):
        return message
    if isinstance(message, str):
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


class ShellyReader:
    """Keeps a connection to the device up and feeds samples to a sink."""

    def __init__(
        self,
        settings: ShellySettings,
        on_samples: Callable[[list[EmSample]], Awaitable[None]],
        on_status: Callable[[bool, str | None], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings
        self._on_samples = on_samples
        self._on_status = on_status
        self._last_frame_at = 0.0

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, read, reconnect. Returns when stop is set.

        Backoff climbs to a minute. Faster than that against a device that is
        off is just noise in the log, and slower means a device that comes back
        after a power cut is not seen for too long.
        """
        delay = 1.0
        while not stop.is_set():
            connection = ShellyConnection(self._settings)
            try:
                await connection.open()
                await self._pump(connection, stop)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except (ShellyError, WebSocketException, OSError, TimeoutError) as error:
                await self._report(False, str(error))
                log.warning(
                    "Shelly at %s: %s. Retrying in %.0fs",
                    self._settings.host,
                    error,
                    delay,
                )
            finally:
                await connection.close()

            if stop.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            delay = min(delay * 2, 60.0)

    async def _pump(self, connection: ShellyConnection, stop: asyncio.Event) -> None:
        # This request is what makes the device start sending notifications, as
        # well as proving the password. Its answer is the current reading, so
        # the dashboard is populated before the first change arrives rather than
        # showing nothing until the pump next runs.
        info = await connection.request("Shelly.GetDeviceInfo")
        log.info(
            "Shelly %s, firmware %s",
            info.get("model") or info.get("id") or "unknown",
            info.get("ver") or "unknown",
        )
        await self._report(True, None)
        await self._seed_current_readings(connection)

        self._last_frame_at = time.monotonic()
        heartbeat = asyncio.create_task(self._heartbeat(connection, stop))
        try:
            async for frame in connection.frames():
                self._last_frame_at = time.monotonic()
                method = frame.get("method")
                if method in ("NotifyStatus", "NotifyFullStatus"):
                    samples = parse_notify_status(frame)
                    if samples:
                        await self._on_samples(samples)
                elif method == "NotifyEvent":
                    _log_events(frame)
                if stop.is_set():
                    return
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _seed_current_readings(self, connection: ShellyConnection) -> None:
        now = datetime.now(UTC)
        samples = []
        for channel in (0, 1):
            status = await connection.request("EM1.GetStatus", {"id": channel})
            if status:
                samples.append(EmSample.from_status(channel, status, now))
        if samples:
            await self._on_samples(samples)

    async def _heartbeat(self, connection: ShellyConnection, stop: asyncio.Event) -> None:
        """Notice a socket that is open but has stopped carrying anything.

        A device that has crashed, or a NAT that has dropped the mapping,
        leaves a socket that reads as fine and never delivers again. The only
        way to tell that apart from a pump that simply has not run is to ask.
        """
        interval = float(self._settings.heartbeat_s)
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
            if stop.is_set():
                return
            if time.monotonic() - self._last_frame_at < interval:
                continue
            # Raising out of here would not reach the reader loop, so a failure
            # closes the socket and lets the read side end instead.
            try:
                await connection.request("EM1.GetStatus", {"id": 0})
                self._last_frame_at = time.monotonic()
            except (ShellyError, WebSocketException, OSError) as error:
                log.warning("Shelly heartbeat failed, reconnecting: %s", error)
                await connection.close()
                return

    async def _report(self, online: bool, error: str | None) -> None:
        if self._on_status is not None:
            await self._on_status(online, error)


def _log_events(frame: dict) -> None:
    """Log the device's own events.

    These are things like a restart or a component error. Nothing reads them
    yet; they are logged so that a gap in the readings can be explained later.
    """
    for event in (frame.get("params") or {}).get("events", []):
        if isinstance(event, dict):
            log.info(
                "Shelly event %s on %s", event.get("event", "?"), event.get("component", "device")
            )


async def probe(settings: ShellySettings) -> dict:
    """Work out what a device is doing, one layer at a time.

    This is what the setup wizard's test button calls, and it deliberately does
    not answer with a single yes or no. "Could not connect" is not a diagnosis,
    and the person reading it is usually standing in a boiler room. So it walks
    up the stack and reports every rung:

    1. Does the name resolve? The single most common failure is an mDNS name
       like `shellyemg3-a1b2c3.local`, which the host resolves and a container
       cannot, because Docker's DNS does not do multicast. That looks like the
       network being broken when it is only the name.
    2. Does a TCP connection open? Separates routing and firewalls from
       everything above them.
    3. Does HTTP RPC answer? Proves the device is a Shelly and is willing to
       talk, and surfaces an authentication requirement as itself rather than as
       a timeout.
    4. Does the websocket work? This is the one ingest actually uses. If HTTP
       works and this does not, the problem is a proxy or a firmware setting,
       not the network, and saying so saves an hour.

    Every rung that passed is reported even when a later one fails, because
    where it stops is the diagnosis.
    """
    host = settings.host.strip()
    steps: list[dict] = []

    def record(name: str, ok: bool, detail: str) -> None:
        steps.append({"step": name, "ok": ok, "detail": detail})

    def answer(ok: bool, error: str | None = None, **extra) -> dict:
        return {"ok": ok, "steps": steps, "error": error, **extra}

    # 1. Name resolution.
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 80, proto=socket.IPPROTO_TCP)
        addresses = sorted({info[4][0] for info in infos})
        record("Resolve the address", True, ", ".join(addresses))
    except socket.gaierror as error:
        hint = ""
        if host.endswith(".local"):
            hint = (
                " Names ending in .local are mDNS, which resolves on your "
                "machine but not inside a container. Use the IP address, or "
                "give the device a DHCP reservation and a real DNS name."
            )
        record("Resolve the address", False, f"{error}.{hint}")
        return answer(False, f"Could not resolve {host}.{hint}")

    # 2. TCP.
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 80), timeout=CONNECT_TIMEOUT_S
        )
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        record("Open a connection to port 80", True, "connected")
    except TimeoutError:
        record("Open a connection to port 80", False, "timed out")
        return answer(
            False,
            f"The name resolved but {host} never answered on port 80. A timeout "
            "here, on a device your Docker host can reach, almost always means "
            "the container cannot get out to your LAN rather than that the "
            "device is down. See 'When the container cannot reach your devices' "
            "in the README.",
        )
    except OSError as error:
        record("Open a connection to port 80", False, str(error))
        return answer(False, f"Could not connect to {host}: {error}")

    # 3. HTTP RPC. Authentication shows up here as a 401 rather than as a
    #    puzzling websocket failure later.
    try:
        # trust_env=False for the same reason the websocket sets proxy=None:
        # this is a device on the local network, and routing it through
        # whatever HTTP_PROXY happens to be set is never what was meant. It
        # also keeps this check and the websocket taking the same path, so one
        # succeeding while the other fails means something real.
        async with httpx2.AsyncClient(timeout=REQUEST_TIMEOUT_S, trust_env=False) as client:
            response = await client.post(
                f"http://{host}/rpc",
                json={"id": 1, "src": CLIENT_ID, "method": "Shelly.GetDeviceInfo"},
            )
        if response.status_code == 401:
            record("Ask over HTTP", False, "the device wants a password")
            return answer(
                False,
                "The Shelly is asking for a password. Put it in the device "
                "password box; the user name is always admin.",
            )
        body = response.json()
        info = body.get("result") or {}
        record(
            "Ask over HTTP",
            True,
            f"{info.get('model') or info.get('id') or 'answered'}, "
            f"firmware {info.get('ver') or 'unknown'}",
        )
    except httpx2.HTTPError as error:
        record("Ask over HTTP", False, str(error))
        return answer(
            False,
            f"{host} accepted a connection but did not answer a Shelly RPC "
            f"request: {error}. Is this really a Shelly?",
        )
    except ValueError as error:
        record("Ask over HTTP", False, f"the reply was not JSON: {error}")
        return answer(False, f"{host} answered, but not with JSON. Is this really a Shelly?")

    # 4. The websocket, which is the one that matters.
    connection = ShellyConnection(settings)
    try:
        await connection.open()
        info = await connection.request("Shelly.GetDeviceInfo")
        record("Open the websocket", True, "connected and answered")

        channels = {}
        for channel in (0, 1):
            status = await connection.request("EM1.GetStatus", {"id": channel})
            channels[channel] = {
                "current": status.get("current"),
                "voltage": status.get("voltage"),
                "act_power": status.get("act_power"),
                "pf": status.get("pf"),
                "errors": status.get("errors") or [],
            }
        record("Read both clamps", True, "both answered")
        return answer(
            True,
            None,
            model=info.get("model"),
            id=info.get("id"),
            firmware=info.get("ver"),
            channels=channels,
        )
    except ShellyAuthError as error:
        record("Open the websocket", False, str(error))
        return answer(False, str(error))
    except InvalidStatus as error:
        # The device answered the upgrade and refused it, which is a different
        # thing from never answering, and the status says which.
        status = error.response.status_code
        record("Open the websocket", False, f"the device answered the upgrade with {status}")
        return answer(
            False,
            f"{host} refused the websocket upgrade with HTTP {status}. HTTP RPC "
            "worked, so the device is reachable and this is the device or "
            "something in front of it declining the upgrade specifically.",
        )
    except (ProxyError, InvalidProxy, InvalidProxyStatus) as error:
        record("Open the websocket", False, f"a proxy refused it: {error}")
        return answer(
            False,
            "A proxy refused to tunnel the websocket. PitWatch does not use a "
            "proxy for devices on your network, so if you are seeing this, "
            f"something is intercepting the connection to {host}.",
        )
    except (ShellyError, WebSocketException, OSError, TimeoutError) as error:
        record("Open the websocket", False, f"{type(error).__name__}: {error}")
        return answer(
            False,
            f"HTTP worked but the websocket did not: {type(error).__name__}: "
            f"{error}. The device answers ordinary requests, so the address and "
            "the network are fine, and this is specific to the upgrade.",
        )
    finally:
        await connection.close()
