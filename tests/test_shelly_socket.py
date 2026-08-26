"""The Shelly connection against a real websocket server that behaves like one.

This exists because of a bug that every unit test missed and that broke the
feature completely: `request()` waited on a future that only the notification
loop could resolve, and the notification loop was not running yet, because it
does not start until after the first request. The socket connected perfectly
and then every call timed out.

Nothing that mocked the transport would have caught it. What catches it is a
server on a real port that answers, so the tests here start one.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from pitwatch.ingest.shelly import ShellyConnection, ShellyError
from pitwatch.schemas import ShellySettings

DEVICE_INFO = {"model": "S3EM-002CXCEU", "id": "shellyemg3-test", "ver": "2.0.0"}


class FakeShelly:
    """A websocket server that answers RPC the way a Gen3 device does."""

    def __init__(self, *, answer: bool = True, push_after_request: bool = False) -> None:
        self.answer = answer
        self.push_after_request = push_after_request
        self.requests: list[dict] = []
        self._server: websockets.Server | None = None
        self.port = 0

    async def __aenter__(self) -> FakeShelly:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = next(iter(self._server.sockets)).getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, socket) -> None:
        async for message in socket:
            frame = json.loads(message)
            self.requests.append(frame)
            if not self.answer:
                continue

            method = frame.get("method")
            if method == "Shelly.GetDeviceInfo":
                result = DEVICE_INFO
            elif method == "EM1.GetStatus":
                channel = (frame.get("params") or {}).get("id", 0)
                result = {"id": channel, "current": 7.0 + channel, "voltage": 121.0}
            else:
                result = {}
            await socket.send(
                json.dumps({"id": frame["id"], "dst": frame["src"], "result": result})
            )

            if self.push_after_request:
                await socket.send(
                    json.dumps(
                        {
                            "src": "shellyemg3-test",
                            "dst": frame["src"],
                            "method": "NotifyStatus",
                            "params": {"ts": 1.0, "em1:0": {"current": 9.5}},
                        }
                    )
                )


def settings_for(port: int) -> ShellySettings:
    return ShellySettings(enabled=True, host=f"127.0.0.1:{port}")


async def test_a_request_gets_its_answer_without_anyone_reading_notifications():
    """The regression. This is the one that was broken.

    Nothing iterates frames() here, exactly as in the real startup path, where
    the first request happens before the notification loop begins.
    """
    async with FakeShelly() as device:
        connection = ShellyConnection(settings_for(device.port))
        await connection.open()
        try:
            info = await connection.request("Shelly.GetDeviceInfo")
        finally:
            await connection.close()

    assert info["model"] == "S3EM-002CXCEU"
    assert info["ver"] == "2.0.0"


async def test_several_requests_in_a_row_each_get_the_right_answer():
    """Replies are matched by id, so a mix-up shows up as swapped clamps."""
    async with FakeShelly() as device:
        connection = ShellyConnection(settings_for(device.port))
        await connection.open()
        try:
            first = await connection.request("EM1.GetStatus", {"id": 0})
            second = await connection.request("EM1.GetStatus", {"id": 1})
        finally:
            await connection.close()

    assert first["current"] == 7.0
    assert second["current"] == 8.0


async def test_concurrent_requests_do_not_cross():
    async with FakeShelly() as device:
        connection = ShellyConnection(settings_for(device.port))
        await connection.open()
        try:
            both = await asyncio.gather(
                connection.request("EM1.GetStatus", {"id": 0}),
                connection.request("EM1.GetStatus", {"id": 1}),
            )
        finally:
            await connection.close()

    assert [reading["id"] for reading in both] == [0, 1]


async def test_every_request_carries_a_src_so_the_device_will_send_notifications():
    """The device sends nothing until it has been told who is asking."""
    async with FakeShelly() as device:
        connection = ShellyConnection(settings_for(device.port))
        await connection.open()
        try:
            await connection.request("Shelly.GetDeviceInfo")
        finally:
            await connection.close()

    assert device.requests[0]["src"] == "pitwatch"
    assert device.requests[0]["method"] == "Shelly.GetDeviceInfo"


async def test_notifications_arrive_while_requests_are_being_made():
    async with FakeShelly(push_after_request=True) as device:
        connection = ShellyConnection(settings_for(device.port))
        await connection.open()
        try:
            await connection.request("Shelly.GetDeviceInfo")
            frames = connection.frames()
            frame = await asyncio.wait_for(anext(frames), timeout=5)
        finally:
            await connection.close()

    assert frame["method"] == "NotifyStatus"
    assert frame["params"]["em1:0"]["current"] == 9.5


async def test_a_device_that_never_answers_times_out_rather_than_hanging():
    async with FakeShelly(answer=False) as device:
        connection = ShellyConnection(settings_for(device.port))
        await connection.open()
        try:
            with pytest.raises(ShellyError, match="timed out"):
                await asyncio.wait_for(connection.request("Shelly.GetDeviceInfo"), timeout=30)
        finally:
            await connection.close()


async def test_closing_releases_a_request_rather_than_making_it_wait_it_out():
    """A dropped connection must not cost the full request timeout.

    The reconnect loop cannot get going again while a request is still sitting
    on a future for an answer that cannot arrive.
    """
    async with FakeShelly(answer=False) as device:
        connection = ShellyConnection(settings_for(device.port))
        await connection.open()

        pending = asyncio.ensure_future(connection.request("Shelly.GetDeviceInfo"))
        await asyncio.sleep(0.1)
        await connection.close()

        with pytest.raises(ShellyError, match="closed"):
            await asyncio.wait_for(pending, timeout=5)


async def test_frames_ends_when_the_connection_closes():
    """Otherwise the reader loop waits forever on a queue nobody will fill."""
    async with FakeShelly() as device:
        connection = ShellyConnection(settings_for(device.port))
        await connection.open()
        frames = connection.frames()
        await connection.close()

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(frames), timeout=5)
