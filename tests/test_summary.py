"""What leaves the building, and what it is worth once it gets there.

The numbers are built here and read by a model somewhere else, so the tests
that matter are about the shape of the page it is handed: that the rain is on
it, that the days line up, and that nothing on it points at the building.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest

from pitwatch import summary
from pitwatch.notify import email as email_sender
from pitwatch.notify import sms as sms_sender
from pitwatch.schemas import (
    ClampSource,
    MqttSettings,
    SiteSettings,
    SummarySettings,
    WeatherSettings,
)

# Two decimal places, about a kilometer, which is what the settings page keeps.
BROOKLYN = {"latitude": 40.68, "longitude": -73.99}


def _app(pool, store) -> SimpleNamespace:
    """What `facts` actually reaches for, and nothing else."""
    return SimpleNamespace(state=SimpleNamespace(pool=pool, settings=store, history=None))


async def _site(store, **overrides):
    await store.put(SiteSettings(timezone="America/New_York", **BROOKLYN, **overrides))


async def _rain(pool, when: datetime, mm: float) -> None:
    await pool.execute(
        """
        INSERT INTO weather_hour (ts, precipitation, fetched_at)
        VALUES ($1, $2, now())
        ON CONFLICT (ts) DO UPDATE SET precipitation = excluded.precipitation
        """,
        when,
        mm,
    )


async def test_the_rain_goes_with_the_numbers(pool, store):
    """The one thing in here from outside the building. Forty calls in a dry
    week and forty in two inches of rain are two different findings, and
    without this column the model cannot tell them apart."""
    await _site(store)
    await store.put(WeatherSettings(enabled=True, units="in"))
    yesterday = datetime.now(UTC) - timedelta(hours=20)
    await _rain(pool, yesterday.replace(minute=0, second=0, microsecond=0), 12.7)

    rain = await summary.rainfall(pool, store, summary.WINDOW, "America/New_York")

    assert rain is not None
    assert rain["units"] == "in"
    # Half an inch, converted at the edge the way every page does it.
    assert sum(day["rain"] for day in rain["days"]) == 0.5
    # Said out loud, because a model handed a rainfall column will otherwise
    # write about it as though somebody had read a gauge.
    assert "not a rain gauge" in rain["source"]


async def test_no_coordinates_is_not_a_dry_week(pool, store):
    """Nothing to ask about reads as nothing to say. Sending a column of zeroes
    would have the model explaining a drought that never happened."""
    await store.put(SiteSettings(timezone="America/New_York"))
    await store.put(WeatherSettings(enabled=True))

    assert await summary.rainfall(pool, store, summary.WINDOW, "America/New_York") is None


async def test_rain_turned_off_sends_none_of_it(pool, store):
    await _site(store)
    await store.put(WeatherSettings(enabled=False))
    await _rain(pool, datetime.now(UTC) - timedelta(hours=3), 5.0)

    assert await summary.rainfall(pool, store, summary.WINDOW, "America/New_York") is None


async def test_a_wet_day_and_a_busy_day_are_the_same_day(pool, store):
    """The whole point of sending rain, and it only works if the days agree.

    Cut on UTC midnight, a run at nine in the evening in New York lands on
    tomorrow and the rain that caused it lands on today, and the model is
    handed two columns that cannot be read across. Both are cut on the site's
    own midnight instead."""
    await _site(store)
    await store.put(WeatherSettings(enabled=True, units="mm"))
    await store.put(
        MqttSettings(
            enabled=True,
            host="broker",
            clamps=[ClampSource(pump=1, topic="meter/em1:0", path="current")],
        )
    )

    # Nine in the evening in New York, which is the small hours of the next day
    # in UTC. Yesterday, so it is inside the window whatever hour it is now.
    evening = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=1, minute=0, second=0, microsecond=0
    )
    await _rain(pool, evening, 8.0)
    await pool.execute(
        """
        INSERT INTO pump_run (pump, started_at, ended_at, duration_s, role, started_by)
        VALUES (1, $1::timestamptz, $1::timestamptz + interval '12 seconds', 12, 'lead', 'contact')
        """,
        evening,
    )

    numbers = await summary.facts(_app(pool, store))

    rain_days = {day["day"] for day in numbers["rain"]["days"] if day["rain"]}
    run_days = {day["day"] for pump in numbers["pumps"] for day in pump["days"] if day.get("runs")}
    assert rain_days and rain_days == run_days


async def test_the_page_the_model_gets_names_no_place(pool, store):
    """The address is the one thing the owner of this pit has been clear about,
    and the rain is the part most likely to carry it: it is asked for by
    coordinate. It leaves as a daily total with no coordinate on it."""
    await _site(store, name="14 Example Street", address="14 Example Street, Brooklyn NY")
    await store.put(WeatherSettings(enabled=True))
    await _rain(pool, datetime.now(UTC) - timedelta(hours=5), 3.0)

    numbers = await summary.facts(_app(pool, store))
    body = summary.messages(store.summary, numbers)[1]["content"]

    for leaked in ("Example Street", "Brooklyn", "40.68", "-73.99", "latitude"):
        assert leaked not in body, leaked


async def test_a_device_error_stays_on_the_diagnostics_page(pool, store):
    """Online and when, and not the error text.

    `last_error` is written by the broker client, so it carries addresses and
    library wording, and this summary is about pumps, amps and contacts. What a
    reader of it needs is that a device was not answering, which `online` says.
    """
    await _site(store)
    # The row is already there: device_status is seeded, so this is an update.
    await pool.execute("DELETE FROM device_status WHERE device <> 'health0'")
    await pool.execute(
        """
        INSERT INTO device_status (device, online, last_seen, last_error)
        VALUES ('health0', false, now(), $1)
        ON CONFLICT (device) DO UPDATE SET online = false, last_seen = now(),
                                           last_error = excluded.last_error
        """,
        "[Errno 111] Connect call failed ('10.136.1.36', 1884)",
    )

    numbers = await summary.facts(_app(pool, store))
    body = summary.messages(store.summary, numbers)[1]["content"]

    assert numbers["devices"] == [{"device": "health0", "online": False, "last_seen": mock.ANY}]
    assert "10.136.1.36" not in body
    assert "Errno" not in body


async def _write(pool, body: str, context: str, minutes_ago: int) -> int:
    return await pool.fetchval(
        """
        INSERT INTO summary (created_at, window_key, model, body, facts, context, written_by)
        VALUES (now() - make_interval(mins => $1), '7d', 'gpt-4o-mini', $2, '{}'::jsonb, $3, 'david')
        RETURNING id
        """,
        minutes_ago,
        body,
        context,
    )


async def test_the_history_lists_every_check_including_the_latest(pool):
    """The two tabs are read at different moments, and a history missing its own
    most recent entry is a history somebody has to reconcile in their head."""
    await _write(pool, "Oldest.", "First words.", 300)
    await _write(pool, "Middle.", "Second words.", 200)
    await _write(pool, "Newest.", "Third words.", 10)

    rows = await summary.every(pool)

    assert [row["body"] for row in rows] == ["Newest.", "Middle.", "Oldest."]


async def test_what_a_check_was_told_is_still_kept_even_though_no_page_shows_it(pool):
    """The prompt came off both pages on 2026-09-09 and is still stored, which
    is not an oversight: a reading a month old is an opinion unless what it was
    looking at, numbers and description alike, is beside it."""
    await _write(pool, "Both pumps look normal.", "Two pumps in a pit.", 60)

    assert (await summary.latest(pool))["context"] == "Two pumps in a pit."


# -- the daily one ------------------------------------------------------------


def _at(hour: int, minute: int = 0) -> datetime:
    """That time in New York, as the UTC instant it happens at."""
    from zoneinfo import ZoneInfo

    here = datetime.now(ZoneInfo("America/New_York")).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return here.astimezone(UTC)


class _App:
    """Everything DailyCheck reaches for, and nothing else."""

    def __init__(self, pool, store):
        self.state = SimpleNamespace(pool=pool, settings=store, history=None)


async def test_the_schedule_does_nothing_until_it_is_switched_on(pool, store):
    from pitwatch.domain.checkup import DailyCheck

    await store.put(SummarySettings(api_key="sk-test", model="m", daily=False))

    assert await DailyCheck(_App(pool, store)).tick(_at(9)) is False


async def test_the_schedule_waits_for_the_hour_on_the_buildings_clock(pool, store):
    """Seven in the morning is seven in the morning where the pit is, not where
    the server happens to be."""
    from pitwatch.domain.checkup import DailyCheck

    await _site(store)
    await store.put(SummarySettings(api_key="sk-test", model="m", daily=True, daily_at="07:00"))

    assert await DailyCheck(_App(pool, store)).tick(_at(6, 30)) is False


async def test_one_a_day_is_decided_by_the_last_one_and_not_by_a_timer(pool, store):
    """A process that remembers in memory forgets on every deploy, and this is
    deployed several times on a busy afternoon. The last check's own timestamp
    answers it across restarts."""
    from pitwatch.domain.checkup import DailyCheck

    await _site(store)
    await store.put(SummarySettings(api_key="sk-test", model="m", daily=True, daily_at="07:00"))
    await _write(pool, "This morning's.", "Two pumps in a pit.", 60)

    assert await DailyCheck(_App(pool, store)).tick(_at(9)) is False


async def test_a_check_whose_hour_passed_while_it_was_down_still_runs(pool, store, monkeypatch):
    """Late rather than never. Yesterday's being the newest one you have is
    worse than one arriving at ten past nine."""
    from pitwatch.domain.checkup import DailyCheck

    await _site(store)
    await store.put(SummarySettings(api_key="sk-test", model="m", daily=True, daily_at="07:00"))
    await _write(pool, "Yesterday's.", "Two pumps in a pit.", 60 * 30)

    async def answer(settings, payload):
        return "Both pumps look normal."

    # Nothing in this suite talks to a model. The half being tested here is
    # which day it is, not what comes back.
    monkeypatch.setattr(summary, "ask", answer)

    assert await DailyCheck(_App(pool, store)).tick(_at(9)) is True

    written = await summary.latest(pool)
    assert written["body"] == "Both pumps look normal."
    assert written["written_by"] == "the schedule", "no account ran it, so none is named"


async def test_the_daily_one_is_emailed_and_not_texted(pool, store, monkeypatch):
    """Four paragraphs of prose is several text messages, arriving every
    morning, on the channel that exists here for two in the morning."""
    from pitwatch.domain.checkup import DailyCheck

    await _site(store)
    await store.put(
        SummarySettings(api_key="sk-test", model="m", daily=True, daily_at="07:00", notify=True)
    )
    await pool.execute(
        """
        INSERT INTO app_user (username, name, email, phone, notify_email, notify_sms,
                              min_severity, enabled)
        VALUES ('super', 'Alex', 'alex@example.com', '+12125550142', true, true, 'info', true)
        """
    )

    async def answer(settings, payload):
        return "Both pumps look normal."

    sent = []

    async def by_email(settings, to, subject, body):
        sent.append((to, subject, body))
        return "queued"

    monkeypatch.setattr(summary, "ask", answer)
    monkeypatch.setattr(email_sender, "send", by_email)
    monkeypatch.setattr(sms_sender, "send", _refuse_to_text)

    assert await DailyCheck(_App(pool, store)).tick(_at(9)) is True

    assert len(sent) == 1
    to, subject, body = sent[0]
    assert to == "alex@example.com"
    assert "health check" in subject
    assert body == "Both pumps look normal."

    rows = await pool.fetch("SELECT alert_id, event, channel, status FROM notification")
    assert [dict(row) for row in rows] == [
        {"alert_id": None, "event": "written", "channel": "email", "status": "sent"}
    ]


async def _refuse_to_text(settings, to, message):
    raise AssertionError("a health check does not go out as a text message")


def test_the_time_of_day_has_to_be_a_time_of_day():
    for bad in ("25:00", "07:99", "seven", "7"):
        with pytest.raises(ValueError, match="HH:MM"):
            SummarySettings(daily_at=bad)

    assert SummarySettings(daily_at="7:5").daily_at == "07:05"
    assert SummarySettings(daily_at="23:59").daily_hour_and_minute == (23, 59)


# -- what it takes to be ready ------------------------------------------------


def test_a_model_on_this_network_is_asked_without_a_key():
    """Requiring one meant an installation pointed at its own hardware saw a
    page saying "add an OpenAI key" and a button that never appeared."""
    for address in (
        "http://127.0.0.1:11434/v1",
        "http://localhost:8080/v1",
        "http://192.168.1.40:1234/v1",
        "http://10.136.1.36:8000/v1",
        "http://172.16.4.4/v1",
        "http://workstation/v1",
        "http://tower.local:5000/v1",
    ):
        settings = SummarySettings(model="llama3", base_url=address, api_key="")
        assert settings.asks_this_network, address
        assert settings.ready, address


def test_anything_out_on_the_internet_still_needs_one():
    """Including a fresh install, where the model and the address are filled in
    by default and the key is the one thing nobody has typed yet."""
    assert not SummarySettings().api_key
    assert not SummarySettings().ready, "the defaults point at OpenAI"

    for address in ("https://api.openai.com/v1", "https://models.example.com/v1"):
        settings = SummarySettings(model="gpt-4o-mini", base_url=address, api_key="")
        assert not settings.asks_this_network, address
        assert not settings.ready, address
        assert SummarySettings(model="gpt-4o-mini", base_url=address, api_key="sk-x").ready


def test_an_address_it_cannot_place_is_treated_as_the_internet():
    """The safe way round. Being wrong here costs a page asking for a key that
    was not needed; the other way round is a button that fails at the far end of
    a request."""
    assert not SummarySettings(model="m", base_url="nonsense", api_key="").ready
    assert not SummarySettings(model="m", base_url="", api_key="").ready
