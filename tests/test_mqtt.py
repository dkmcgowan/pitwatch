"""One broker connection, routing whatever arrives to whoever asked for it.

No broker and no hardware. The connection itself is aiomqtt's problem; what is
worth testing is the part this file invented: which row a delivered message
belongs to, what gets made of it, when something is asked rather than waited
for, and when silence counts as a fault.
"""

from __future__ import annotations

import asyncio

import pytest

from pitwatch.ingest.mqtt import MqttReader, topic_matches
from pitwatch.schemas import ClampSource, ContactInput, HealthSource, MqttSettings

# Captured off the real broker on 2026-09-07.
STATUS_FRAME = '{"id":0,"voltage":117.6,"current":16.714,"act_power":-1899.8}'
RPC_REPLY = '{"id":1,"src":"meter","dst":"pitwatch-c1","result":{"id":0,"current":15.487}}'


def _settings(**extra) -> MqttSettings:
    fields = {
        "enabled": True,
        "clamps": [
            ClampSource(
                pump=1,
                topic="meter/status/em1:0",
                path="current",
                channel=0,
                ask_topic="meter/rpc",
                ask_payload='{"id":1,"src":"pitwatch-c1","method":"EM1.GetStatus"}',
                reply_topic="pitwatch-c1/rpc",
                reply_path="result.current",
                ask_while_running=True,
            ),
            ClampSource(pump=2, topic="meter/status/em1:1", path="current", channel=1),
        ],
        "inputs": [
            ContactInput(channel=1, role="pump1_run", topic="pit/in/1"),
            ContactInput(channel=2, role="pump2_run", topic="pit/in/2"),
            ContactInput(channel=3, role="system_alert", topic="pit/in/3", invert=True),
        ],
        "health": [HealthSource(name="Panel module", topic="pit/heartbeat", expect_s=60)],
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

    async def on_status(self, key, online, error) -> None:
        self.status.append((key, online, error))


def _reader(settings=None) -> tuple[MqttReader, _Caught]:
    caught = _Caught()
    reader = MqttReader(
        settings or _settings(),
        on_events=caught.on_events,
        on_samples=caught.on_samples,
        on_status=caught.on_status,
    )
    return reader, caught


# -- which row a message belongs to ------------------------------------------


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


# -- clamps ------------------------------------------------------------------


async def test_a_reading_is_filed_under_the_channel_its_clamp_names():
    """The channel is a setting rather than the pump number, because readings
    already stored are filed under whatever the meter called its clamps."""
    reader, caught = _reader()

    await reader._handle(_Message("meter/status/em1:0", STATUS_FRAME))

    assert len(caught.samples) == 1
    assert caught.samples[0].current == pytest.approx(16.714)
    assert caught.samples[0].channel == 0


async def test_a_reply_is_read_at_its_own_path():
    """The answer to a forced reading arrives on a different topic in a
    different envelope, and both are settings."""
    reader, caught = _reader()

    await reader._handle(_Message("pitwatch-c1/rpc", RPC_REPLY))

    assert len(caught.samples) == 1
    assert caught.samples[0].current == pytest.approx(15.487)
    assert caught.samples[0].channel == 0, "the pump that asked"


async def test_a_frame_with_nothing_at_the_path_produces_nothing():
    """A meter publishes voltage-only and system frames on the same wire."""
    reader, caught = _reader()

    await reader._handle(_Message("meter/status/em1:0", '{"id":0,"voltage":118.1}'))

    assert caught.samples == []


async def test_a_body_that_is_not_json_costs_its_own_message_and_nothing_else():
    """A body this cannot read is a configuration problem on the device. The
    next message may be fine, and the one after that is the one about the
    flood."""
    reader, caught = _reader()

    await reader._handle(_Message("meter/status/em1:0", "<html>no</html>"))
    await reader._handle(_Message("meter/status/em1:0", STATUS_FRAME))

    assert len(caught.samples) == 1, "the good one still landed"


# -- contacts ----------------------------------------------------------------


async def test_a_contact_on_its_own_topic_becomes_an_event():
    reader, caught = _reader()

    await reader._handle(_Message("pit/in/1", "1"))

    assert [(event.channel, event.state) for event in caught.events] == [(1, True)]
    assert caught.events[0].label == "Pump 1 running"


async def test_the_inversion_is_applied_per_input():
    """Channel 3 is a fail safe signal: it holds voltage while all is well and
    drops it on the event, so the raw reading means the opposite."""
    reader, caught = _reader()

    await reader._handle(_Message("pit/in/3", "on"))

    assert [(event.channel, event.state) for event in caught.events] == [(3, False)]


async def test_the_same_state_twice_is_not_two_events():
    reader, caught = _reader()

    await reader._handle(_Message("pit/in/1", "on"))
    await reader._handle(_Message("pit/in/1", "on"))

    assert len(caught.events) == 1


async def test_a_contact_that_publishes_nonsense_is_logged_and_dropped():
    """None is not False. Recording a lamp as off because a body could not be
    read is an alarm that will never fire and will look like it is working."""
    reader, caught = _reader()

    await reader._handle(_Message("pit/in/1", "maybe"))

    assert caught.events == []


async def test_an_input_with_no_topic_hears_nothing():
    """Under the combined body a role was enough, because every input arrived
    whatever it was called. With a topic each, an input nobody has given one is
    an input nothing will ever arrive for."""
    settings = _settings(inputs=[ContactInput(channel=1, role="pump1_run", topic="")])

    assert settings.used_inputs == []
    reader, _ = _reader(settings)
    assert "pit/in/1" not in reader._subscriptions()


# -- asking ------------------------------------------------------------------


class _Publisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic, payload) -> None:
        self.published.append((topic, payload))


def test_nothing_is_asked_of_a_clamp_with_nothing_to_ask():
    """A contact module publishes when a contact moves and there is no question
    to put to it in between. The second clamp here has no ask either."""
    settings = _settings()

    assert settings.clamp_of(1).asks is True
    assert settings.clamp_of(2).asks is False


async def test_a_clamp_is_asked_only_while_a_pump_is_turning():
    """A meter publishes on change, so a motor running steady produces nothing.
    The panel's own run contact says when that is, so a pit sitting still is a
    pit nothing is polling."""
    reader, _ = _reader()
    publisher = _Publisher()
    reader._client = publisher
    clamp = _settings().clamp_of(1)

    reader.watch_run(False)
    assert reader._running.is_set() is False

    reader.watch_run(True)
    await reader._ask(clamp)

    assert publisher.published == [(clamp.ask_topic, clamp.ask_payload)]


async def test_asking_a_broker_that_has_gone_costs_the_ask_and_not_the_reader():
    class _Broken:
        async def publish(self, topic, payload):
            raise OSError("gone")

    reader, _ = _reader()
    reader._client = _Broken()

    await reader._ask(_settings().clamp_of(1))  # does not raise


def test_two_clamps_cannot_read_their_answers_off_one_topic_and_path():
    """An MQTT message carries no sender and no sign of what it is answering,
    so two clamps reading the reply at the same path both match every answer.

    This shipped. One reading would have been filed under both pumps, and it
    would have convinced the history page that the second pump had a clamp
    fitted and drawn a line for a CT that is not installed.
    """
    with pytest.raises(ValueError, match="read their answer from"):
        MqttSettings(
            clamps=[
                ClampSource(
                    pump=1,
                    topic="a",
                    path="current",
                    channel=0,
                    ask_topic="meter/rpc",
                    ask_payload="{}",
                    reply_topic="pitwatch/rpc",
                    reply_path="result.current",
                ),
                ClampSource(
                    pump=2,
                    topic="b",
                    path="current",
                    channel=1,
                    ask_topic="meter/rpc",
                    ask_payload="{}",
                    reply_topic="pitwatch/rpc",
                    reply_path="result.current",
                ),
            ]
        )


def test_one_reply_topic_is_fine_where_the_paths_differ():
    """It is the pair that has to be distinct. Where the paths differ, only one
    clamp finds anything in a given body."""
    settings = MqttSettings(
        clamps=[
            ClampSource(
                pump=1,
                topic="a",
                channel=0,
                ask_topic="m/rpc",
                ask_payload="{}",
                reply_topic="pitwatch/rpc",
                reply_path="params.em1:0.current",
            ),
            ClampSource(
                pump=2,
                topic="b",
                channel=1,
                ask_topic="m/rpc",
                ask_payload="{}",
                reply_topic="pitwatch/rpc",
                reply_path="params.em1:1.current",
            ),
        ]
    )

    assert len(settings.used_clamps) == 2


def test_a_shared_reply_topic_is_subscribed_once():
    """Subscribing twice would deliver every answer twice."""
    settings = MqttSettings(
        clamps=[
            ClampSource(
                pump=1,
                topic="m/em1:0",
                channel=0,
                ask_topic="m/rpc",
                ask_payload="{}",
                reply_topic="pitwatch/rpc",
                reply_path="params.em1:0.current",
            ),
            ClampSource(
                pump=2,
                topic="m/em1:1",
                channel=1,
                ask_topic="m/rpc",
                ask_payload="{}",
                reply_topic="pitwatch/rpc",
                reply_path="params.em1:1.current",
            ),
        ]
    )
    reader, _ = _reader(settings)

    assert reader._subscriptions().count("pitwatch/rpc") == 1


# -- is it still there --------------------------------------------------------


async def test_silence_is_what_says_a_device_is_offline():
    """Not the broker's last will. Measured on the real panel: an unplugged
    meter kept its `online` topic true for the whole outage and published false
    a tenth of a second before it published true again."""
    settings = _settings(health=[HealthSource(name="Panel module", topic="pit/hb", expect_s=1)])
    reader, caught = _reader(settings)
    stop = asyncio.Event()

    reader._heard_at["health0"] = asyncio.get_running_loop().time()
    watcher = asyncio.create_task(reader._watch_silence(stop))
    await asyncio.sleep(3.2)
    stop.set()
    watcher.cancel()

    offline = [row for row in caught.status if row[1] is False]
    assert offline, "a device that stopped speaking is reported offline"
    assert offline[0][0] == "health0"
    assert "expected every 1 s" in offline[0][2]


async def test_a_device_with_no_interval_is_never_held_to_one():
    """Holding silence against a device that was never asked to speak on a
    schedule would paint a permanent red and teach somebody to ignore it."""
    settings = _settings(health=[HealthSource(name="Quiet", topic="pit/hb", expect_s=0)])
    reader, caught = _reader(settings)
    stop = asyncio.Event()

    watcher = asyncio.create_task(reader._watch_silence(stop))
    await asyncio.sleep(0.3)
    stop.set()
    watcher.cancel()

    assert caught.status == []


async def test_a_device_is_only_reported_offline_once():
    """The flag is what stops a silent device being reported once per check for
    the rest of the night."""
    settings = _settings(health=[HealthSource(name="Panel module", topic="pit/hb", expect_s=1)])
    reader, caught = _reader(settings)
    stop = asyncio.Event()

    reader._heard_at["health0"] = asyncio.get_running_loop().time()
    watcher = asyncio.create_task(reader._watch_silence(stop))
    await asyncio.sleep(4.2)
    stop.set()
    watcher.cancel()

    assert len([row for row in caught.status if row[1] is False]) == 1


async def test_a_heartbeat_counts_by_arriving_rather_than_by_what_it_says():
    """A module's own heartbeat body is an id and an uptime with no status
    field in it at all, and reading it for one would mark a healthy module dead
    every sixty seconds."""
    settings = _settings(health=[HealthSource(name="Panel module", topic="pit/hb", expect_s=60)])
    reader, _ = _reader(settings)

    await reader._handle(_Message("pit/hb", '{"id":"x408","upTime":"178845"}'))

    assert "health0" in reader._heard_at


def test_the_contacts_get_no_row_of_their_own_in_device_status():
    """Eight inputs would be eight rows saying the same thing about one module,
    which is what a health check is for."""
    reader, _ = _reader()

    reported = reader._reported()

    assert reported == ["clamp1", "clamp2", "health0"]
