"""One broker connection, routing whatever arrives to whoever asked for it.

No broker and no hardware. The connection itself is aiomqtt's problem; what is
worth testing here is the part this file invented: which source a delivered
message belongs to, what gets made of it, when something is asked rather than
waited for, and when silence counts as a fault.
"""

from __future__ import annotations

import asyncio

import pytest

from pitwatch.ingest.mqtt import MqttReader, topic_matches
from pitwatch.schemas import ChannelMap, MqttSettings, MqttSource

# The two envelopes one meter publishes the same reading in, captured off the
# real broker on 2026-09-07.
STATUS_FRAME = '{"id":0,"voltage":117.6,"current":16.714,"act_power":-1899.8}'
RPC_REPLY = '{"id":1,"src":"meter","dst":"pitwatch","result":{"id":0,"current":15.487}}'
CONTACT_BODY = '{"1":"1","2":"0","3":"1","4":"0","5":"0","6":"0","7":"0","8":"0"}'


def _settings(**extra) -> MqttSettings:
    fields = {
        "enabled": True,
        "sources": [
            MqttSource(
                name="Pump 1 clamp",
                role="clamp1",
                topic="pit/clamps/status/em1:0",
                profile="number",
                path="current",
                channel=0,
                expect_s=45,
                ask_topic="pit/clamps/rpc",
                ask_payload='{"id":1,"src":"pitwatch","method":"EM1.GetStatus"}',
                reply_topic="pitwatch/rpc",
                reply_path="result.current",
                ask_while_running=True,
            ),
            MqttSource(
                name="Panel inputs",
                role="contacts",
                topic="pit/inputs",
                profile="contact_map",
            ),
            MqttSource(
                name="Panel module",
                role="heartbeat",
                topic="pit/heartbeat",
                profile="number",
                expect_s=60,
            ),
        ],
        "channels": [
            ChannelMap(channel=1, role="pump1_run"),
            ChannelMap(channel=2, role="pump2_run"),
            ChannelMap(channel=3, role="system_alert", invert=True),
        ],
    }
    fields.update(extra)
    return MqttSettings(**fields)


class _Message:
    """What aiomqtt hands the reader, with nothing else on it."""

    def __init__(self, topic: str, payload: str) -> None:
        self.topic = topic
        self.payload = payload.encode()


class _Caught:
    """Collects what the reader decided, instead of a database."""

    def __init__(self) -> None:
        self.events: list = []
        self.samples: list = []
        self.status: list[tuple[str, bool, str | None]] = []

    async def on_events(self, events) -> None:
        self.events.extend(events)

    async def on_samples(self, samples) -> None:
        self.samples.extend(samples)

    async def on_status(self, role, online, error) -> None:
        self.status.append((role, online, error))


def _reader(settings=None, caught=None) -> tuple[MqttReader, _Caught]:
    caught = caught or _Caught()
    reader = MqttReader(
        settings or _settings(),
        on_events=caught.on_events,
        on_samples=caught.on_samples,
        on_status=caught.on_status,
    )
    return reader, caught


# -- which source a message belongs to ---------------------------------------


def test_the_wildcards_are_the_brokers_own():
    """The router has to agree with the broker about what a subscription
    covers. The two answering differently would file a float reading under a
    clamp."""
    assert topic_matches("a/b", "a/b")
    assert topic_matches("a/+/c", "a/anything/c")
    assert not topic_matches("a/+/c", "a/b/d")
    assert not topic_matches("a/+", "a/b/c"), "a plus is exactly one level"
    assert topic_matches("a/#", "a/b/c/d")
    assert topic_matches("a/#", "a"), "a hash covers no levels as well as many"
    assert not topic_matches("a/#", "b/c")
    assert not topic_matches("a/b", "a")


# -- readings ----------------------------------------------------------------


async def test_a_status_frame_becomes_a_sample_under_its_own_channel():
    """The channel is a setting rather than the role's number, because the
    readings already stored are filed under whatever the meter called its
    clamps."""
    reader, caught = _reader()

    await reader._handle(_Message("pit/clamps/status/em1:0", STATUS_FRAME))

    assert len(caught.samples) == 1
    assert caught.samples[0].current == pytest.approx(16.714)
    assert caught.samples[0].channel == 0


async def test_a_reply_is_read_at_its_own_path():
    """The answer to a forced reading arrives on a different topic, in a
    different envelope, and both are settings. This is the piece that let the
    last device specific code go."""
    reader, caught = _reader()

    await reader._handle(_Message("pitwatch/rpc", RPC_REPLY))

    assert len(caught.samples) == 1
    assert caught.samples[0].current == pytest.approx(15.487)


async def test_a_frame_with_nothing_at_the_path_produces_nothing():
    """A meter publishes voltage-only and system frames on the same wire, and
    a frame with no reading in it is the device working."""
    reader, caught = _reader()

    await reader._handle(_Message("pit/clamps/status/em1:0", '{"id":0,"voltage":118.1}'))

    assert caught.samples == []


async def test_a_body_that_is_not_json_costs_its_own_message_and_nothing_else():
    """A body this cannot read is a configuration problem on the device. The
    next message may be fine, and the one after that is the one about the
    flood."""
    reader, caught = _reader()

    await reader._handle(_Message("pit/clamps/status/em1:0", "<html>no</html>"))
    await reader._handle(_Message("pit/clamps/status/em1:0", STATUS_FRAME))

    assert len(caught.samples) == 1, "the good one still landed"


async def test_contacts_become_events_with_the_inversion_applied():
    """Channel 3 is a fail safe signal: it holds voltage while all is well and
    drops it on the event, so the raw reading means the opposite."""
    reader, caught = _reader()

    await reader._handle(_Message("pit/inputs", CONTACT_BODY))

    states = {event.channel: event.state for event in caught.events}
    assert states[1] is True, "pump 1 running"
    assert states[3] is False, "system alert reads healthy, inverted from raw 1"


async def test_every_input_is_recorded_whether_or_not_it_has_a_meaning():
    """Recording an input and drawing it are different questions. An input
    nobody has said anything about is still read, debounced and written down;
    it simply has nowhere on a dashboard to be shown. Dropping it here would
    mean the day somebody wires the eighth contact, its history starts empty.
    """
    reader, caught = _reader()

    await reader._handle(_Message("pit/inputs", CONTACT_BODY))

    assert {event.channel for event in caught.events} == {1, 2, 3, 4, 5, 6, 7, 8}
    # And the ones with a meaning carry it, so the log line reads.
    titled = {event.channel: event.label for event in caught.events}
    assert titled[1] == "Pump 1 running"
    assert titled[8] == "DI8", "an input with no role still gets a name to be logged under"


async def test_one_contact_on_its_own_topic_is_renumbered_to_its_input():
    """For hardware that publishes one input per topic. The parser cannot know
    which input a topic is; the setting does."""
    settings = _settings(
        sources=[
            MqttSource(
                name="High water",
                role="contacts",
                topic="pit/float/high",
                profile="contact",
                input_number=2,
            )
        ]
    )
    reader, caught = _reader(settings)

    await reader._handle(_Message("pit/float/high", "on"))

    assert [(event.channel, event.state) for event in caught.events] == [(2, True)]


async def test_the_same_state_twice_is_not_two_events():
    reader, caught = _reader()

    await reader._handle(_Message("pit/inputs", CONTACT_BODY))
    first = len(caught.events)
    await reader._handle(_Message("pit/inputs", CONTACT_BODY))

    assert len(caught.events) == first


# -- asking ------------------------------------------------------------------


class _Publisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic, payload) -> None:
        self.published.append((topic, payload))


async def test_nothing_is_asked_of_a_source_with_nothing_to_ask():
    """A contact module publishes when a contact moves and there is no
    question to put to it in between. This was the whole of the old design for
    the panel inputs and it stays that way."""
    settings = _settings()

    contacts = settings.source_for("contacts")
    heartbeat = settings.source_for("heartbeat")

    assert contacts.asks is False
    assert heartbeat.asks is False
    assert settings.source_for("clamp1").asks is True


async def test_a_source_is_asked_only_while_a_pump_is_turning():
    """A meter publishes on change, so a motor running steady produces
    nothing. The panel's own run contact says when that is, so a pit sitting
    still is a pit nothing is polling."""
    reader, _ = _reader()
    publisher = _Publisher()
    reader._client = publisher

    source = _settings().source_for("clamp1")

    reader.watch_run(False)
    assert reader._running.is_set() is False

    reader.watch_run(True)
    await reader._ask(source)

    assert publisher.published == [(source.ask_topic, source.ask_payload)]


async def test_asking_a_broker_that_has_gone_costs_the_ask_and_not_the_reader():
    class _Broken:
        async def publish(self, topic, payload):
            raise OSError("gone")

    reader, _ = _reader()
    reader._client = _Broken()

    await reader._ask(_settings().source_for("clamp1"))  # does not raise


# -- liveness ----------------------------------------------------------------


async def test_silence_is_what_says_a_source_is_offline():
    """Not the broker's last will. Measured on the real panel: an unplugged
    meter kept its `online` topic true for the whole outage and published false
    a tenth of a second before it published true again."""
    settings = _settings(
        sources=[
            MqttSource(
                name="Panel module",
                role="heartbeat",
                topic="pit/heartbeat",
                profile="number",
                expect_s=1,
            )
        ]
    )
    reader, caught = _reader(settings)
    stop = asyncio.Event()

    reader._heard_at["heartbeat"] = asyncio.get_running_loop().time()
    watcher = asyncio.create_task(reader._watch_silence(stop))
    await asyncio.sleep(3.2)
    stop.set()
    watcher.cancel()

    offline = [row for row in caught.status if row[1] is False]
    assert offline, "a source that stopped speaking is reported offline"
    assert offline[0][0] == "heartbeat"
    assert "expected every 1 s" in offline[0][2]


async def test_a_source_that_never_had_an_interval_is_never_held_to_one():
    """Holding silence against a device that was never asked to speak on a
    schedule would paint a permanent red and teach somebody to ignore it."""
    settings = _settings(
        sources=[
            MqttSource(name="Quiet", role="contacts", topic="pit/inputs", profile="contact_map")
        ]
    )
    reader, caught = _reader(settings)
    stop = asyncio.Event()

    watcher = asyncio.create_task(reader._watch_silence(stop))
    await asyncio.sleep(0.3)
    stop.set()
    watcher.cancel()

    assert caught.status == []


async def test_a_source_that_comes_back_is_only_reported_once_each_way():
    """The flag is what stops a silent source being reported offline once per
    check for the rest of the night."""
    settings = _settings(
        sources=[
            MqttSource(
                name="Panel module",
                role="heartbeat",
                topic="pit/heartbeat",
                profile="number",
                expect_s=1,
            )
        ]
    )
    reader, caught = _reader(settings)
    stop = asyncio.Event()

    reader._heard_at["heartbeat"] = asyncio.get_running_loop().time()
    watcher = asyncio.create_task(reader._watch_silence(stop))
    await asyncio.sleep(4.2)
    stop.set()
    watcher.cancel()

    assert len([row for row in caught.status if row[1] is False]) == 1


# -- subscriptions -----------------------------------------------------------


def test_a_shared_reply_topic_is_subscribed_once():
    """Several sources asking one device get their answers on the same topic,
    and subscribing to it twice would deliver every answer twice."""
    settings = _settings(
        sources=[
            MqttSource(
                role="clamp1",
                topic="pit/clamps/status/em1:0",
                profile="number",
                path="current",
                ask_topic="pit/clamps/rpc",
                ask_payload="{}",
                reply_topic="pitwatch/rpc",
            ),
            MqttSource(
                role="clamp2",
                topic="pit/clamps/status/em1:1",
                profile="number",
                path="current",
                ask_topic="pit/clamps/rpc",
                ask_payload="{}",
                reply_topic="pitwatch/rpc",
            ),
        ]
    )
    reader, _ = _reader(settings)

    wanted = reader._subscriptions()

    assert wanted.count("pitwatch/rpc") == 1
    assert set(wanted) == {"pit/clamps/status/em1:0", "pit/clamps/status/em1:1", "pitwatch/rpc"}


def test_a_source_that_is_half_configured_is_not_subscribed_to():
    """The migration writes the clamp sources with everything filled in except
    a topic, because there never was one to carry over. An empty topic has to
    read as "not configured yet" rather than as a subscription to nothing."""
    settings = _settings(
        sources=[MqttSource(role="clamp1", topic="", profile="number", path="current")]
    )
    reader, _ = _reader(settings)

    assert settings.used_sources == []
    assert reader._subscriptions() == []
