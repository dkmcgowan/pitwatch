"""The Shelly protocol code, without a Shelly.

Everything here is the part that turns frames into readings, which is the part
that can be wrong quietly. The socket handling is not covered; that needs a
device, and it is the half that fails loudly.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from pitwatch.ingest.shelly import (
    AUTH_USERNAME,
    EmSample,
    digest_response,
    parse_notify_status,
)
from pitwatch.ingest.sink import LiveState


def test_notify_status_yields_a_sample_per_clamp():
    frame = {
        "src": "shellyemg3-abc",
        "dst": "pitwatch",
        "method": "NotifyStatus",
        "params": {
            "ts": 1_755_000_000.0,
            "em1:0": {"id": 0, "current": 7.21, "voltage": 121.4, "act_power": 848.2, "pf": 0.97},
            "em1:1": {"id": 1, "current": 0.0, "voltage": 121.2, "act_power": 0.0, "pf": 0.0},
        },
    }

    samples = parse_notify_status(frame)

    assert [sample.channel for sample in samples] == [0, 1]
    assert samples[0].current == 7.21
    assert samples[1].current == 0.0
    assert samples[0].ts == datetime.fromtimestamp(1_755_000_000.0, tz=UTC)


def test_notify_status_ignores_components_that_are_not_clamps():
    frame = {
        "method": "NotifyStatus",
        "params": {
            "ts": 1_755_000_000.0,
            "switch:0": {"output": False},
            "wifi": {"rssi": -61},
            "em1:0": {"current": 1.5},
        },
    }

    samples = parse_notify_status(frame)

    assert len(samples) == 1
    assert samples[0].channel == 0


def test_a_partial_notification_produces_nulls_rather_than_invented_readings():
    """Delta frames carry only what changed.

    A frame that mentions voltage and not current must not be read as "the
    current is now zero". Zero amps is a pump that has stopped, and inventing
    it here would show up as a run that ended when it had not.
    """
    frame = {"method": "NotifyStatus", "params": {"ts": 1.0, "em1:0": {"voltage": 121.0}}}

    sample = parse_notify_status(frame)[0]

    assert sample.voltage == 121.0
    assert sample.current is None


def test_a_notification_with_no_timestamp_is_stamped_on_arrival():
    frame = {"method": "NotifyStatus", "params": {"em1:0": {"current": 2.0}}}

    before = datetime.now(UTC)
    sample = parse_notify_status(frame)[0]

    assert sample.ts >= before


def test_frames_that_are_not_status_notifications_yield_nothing():
    assert parse_notify_status({"method": "NotifyEvent", "params": {"events": []}}) == []
    assert parse_notify_status({"method": "NotifyStatus"}) == []


def test_live_state_carries_forward_fields_a_delta_did_not_mention():
    live = LiveState()
    live.update(EmSample(datetime.now(UTC), 0, 7.2, 121.0, 850.0, 860.0, 0.98, 60.0))

    live.update(EmSample(datetime.now(UTC), 0, None, 120.4, None, None, None, None))

    assert live.current_for(0) == 7.2
    assert live.samples[0].voltage == 120.4
    assert live.samples[0].act_power == 850.0


def test_live_state_keeps_the_two_clamps_apart():
    live = LiveState()
    live.update(EmSample(datetime.now(UTC), 0, 7.2, 121.0, None, None, None, None))
    live.update(EmSample(datetime.now(UTC), 1, 0.0, 121.0, None, None, None, None))

    assert live.current_for(0) == 7.2
    assert live.current_for(1) == 0.0


def test_digest_matches_the_specified_construction():
    """Recompute the digest by hand and compare.

    The construction is unusual enough that a transcription error would look
    plausible, and the only other way to find out is a device refusing to talk.
    """
    challenge = {"realm": "shellyemg3-08f9e0abcdef", "nonce": "1755000000"}
    password = "hunter2hunter2"

    auth = digest_response(challenge, password, cnonce="0123456789abcdef", nc=1)

    def sha(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    ha1 = sha(f"{AUTH_USERNAME}:{challenge['realm']}:{password}")
    ha2 = sha("dummy_method:dummy_uri")
    expected = sha(f"{ha1}:{challenge['nonce']}:00000001:0123456789abcdef:auth:{ha2}")

    assert auth["response"] == expected
    assert auth["nc"] == "00000001"
    assert auth["username"] == "admin"
    assert auth["algorithm"] == "SHA-256"


def test_nonce_count_is_eight_hex_digits():
    auth = digest_response({"realm": "r", "nonce": "n"}, "pw", cnonce="c", nc=255)

    assert auth["nc"] == "000000ff"


def test_the_websocket_is_never_routed_through_a_proxy():
    """A meter on the LAN must not be reached through HTTP_PROXY.

    The websockets library defaults to proxy=True, which reads HTTP_PROXY and
    ALL_PROXY from the environment. That fails in a way that looks like a
    broken device rather than a proxy: plain HTTP to the same address works,
    because a proxy forwards that happily, while the upgrade needs a CONNECT
    tunnel it may refuse. The symptom is "HTTP worked but the websocket did
    not", which sends you looking at the wrong thing entirely.
    """
    source = (Path(__file__).parent.parent / "pitwatch" / "ingest" / "shelly.py").read_text(
        encoding="utf-8"
    )

    assert "proxy=None" in source, "websockets.connect must disable the proxy"
    assert "trust_env=False" in source, "the HTTP probe must disable it too"


def test_every_http_client_ignores_the_environment_proxy():
    """Both checks have to take the same path.

    If the HTTP probe went through a proxy and the websocket did not, one
    succeeding while the other failed would say nothing about the device, which
    is the whole point of checking both.
    """
    source = (Path(__file__).parent.parent / "pitwatch" / "ingest" / "shelly.py").read_text(
        encoding="utf-8"
    )

    for line in source.splitlines():
        if "httpx2.AsyncClient(" in line:
            assert "trust_env=False" in line, line.strip()
