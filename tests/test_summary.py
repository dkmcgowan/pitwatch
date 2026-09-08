"""What leaves the building, and what it is worth once it gets there.

The numbers are built here and read by a model somewhere else, so the tests
that matter are about the shape of the page it is handed: that the rain is on
it, that the days line up, and that nothing on it points at the building.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from pitwatch import summary
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


def _last(created_at, context: str) -> dict:
    return {"created_at": created_at, "context": context, "body": "Both pumps look normal."}


def test_the_first_summary_is_always_allowed():
    settings = SummarySettings(api_key="sk-test", description="Two pumps in a pit.")

    assert summary.offer(settings, None).allowed


def test_the_same_week_and_the_same_words_give_the_same_answer():
    """Which is not worth a second call on somebody's account. The button goes
    gray and says which half is stale rather than disappearing: a control that
    vanishes and comes back is a control nobody learns."""
    settings = SummarySettings(api_key="sk-test", description="Two pumps in a pit.")
    yesterday = datetime.now(UTC) - timedelta(days=1)

    decision = summary.offer(settings, _last(yesterday, "Two pumps in a pit."))

    assert not decision.allowed
    assert "6 days" in decision.because
    assert "change what you have written" in decision.because


def test_a_week_of_readings_it_has_not_seen_opens_the_button():
    settings = SummarySettings(api_key="sk-test", description="Two pumps in a pit.")
    last_week = datetime.now(UTC) - timedelta(days=7, minutes=1)

    assert summary.offer(settings, _last(last_week, "Two pumps in a pit.")).allowed


def test_telling_it_something_new_opens_the_button():
    """The deliberate way through, and not a loophole. A summary is worth
    arguing with, and the way to argue with this one is to tell it the thing it
    did not know."""
    settings = SummarySettings(
        api_key="sk-test",
        description="Two pumps in a pit. The check valve was replaced in the spring.",
    )
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)

    assert summary.offer(settings, _last(an_hour_ago, "Two pumps in a pit.")).allowed


def test_a_summary_written_before_the_context_was_kept_does_not_hold_the_button():
    """Rows from before the column existed carry an empty context, and nothing
    is known about what those were told."""
    settings = SummarySettings(api_key="sk-test", description="Two pumps in a pit.")

    assert summary.offer(settings, _last(datetime.now(UTC), "")).allowed


def test_no_key_is_its_own_answer():
    assert not summary.offer(SummarySettings(), None).allowed


async def test_a_summary_keeps_what_it_was_told_about_the_building(pool, store):
    """So a summary read a month later can be checked against the description it
    was given as well as the readings it saw, and so the page can tell whether
    anything has changed since."""
    await pool.execute(
        """
        INSERT INTO summary (window_key, model, body, facts, context, written_by)
        VALUES ('7d', 'gpt-4o-mini', 'Both pumps look normal.', '{}'::jsonb, $1, 'david')
        """,
        "Two pumps in a pit.",
    )

    last = await summary.latest(pool)

    assert last["context"] == "Two pumps in a pit."


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


async def test_the_earlier_list_is_everything_but_the_one_on_the_page(pool):
    """The latest is printed in full above it, so listing it again would be the
    same paragraph twice."""
    await _write(pool, "Oldest.", "First words.", 300)
    await _write(pool, "Middle.", "Second words.", 200)
    newest = await _write(pool, "Newest.", "Third words.", 10)

    rows = await summary.earlier(pool)

    assert [row["body"] for row in rows] == ["Middle.", "Oldest."]
    assert newest not in [row["id"] for row in rows]
    assert rows[0]["context"] == "Second words."


async def test_the_words_a_summary_was_written_from_can_be_asked_for(pool):
    which = await _write(pool, "Both pumps look normal.", "Two pumps in a pit.", 60)

    assert await summary.told(pool, which) == "Two pumps in a pit."
    # Not the same answer as a row that was never written.
    assert await summary.told(pool, which + 1000) is None


async def test_a_summary_from_before_the_words_were_kept_has_none_to_give_back(pool):
    """Empty, and not None: the row exists and nothing is known about what it
    was told. Restoring it would wipe the description and call it a restore,
    which is why the page checks before it offers."""
    which = await _write(pool, "Nothing worth acting on.", "", 60)

    assert await summary.told(pool, which) == ""
