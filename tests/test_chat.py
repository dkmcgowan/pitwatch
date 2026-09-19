"""What leaves the building, and what comes back.

The numbers are built here and read by a model somewhere else, so the tests
that matter are about the shape of the page it is handed: that the rain is on
it, that the days line up, that every call is on it with the gap before it, and
that nothing on it points at the building.

The other half is the transcript. One thread per person per building, which is
two kinds of isolation and both are asserted: your questions are not ttsang's,
and this building's are not the one next door's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from unittest import mock

import pytest

from pitwatch import chat
from pitwatch.schemas import (
    AiSettings,
    ChatSettings,
    ClampSource,
    MqttSettings,
    SiteSettings,
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

    rain = await chat.rainfall(pool, store, chat.WINDOW, "America/New_York")

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

    assert await chat.rainfall(pool, store, chat.WINDOW, "America/New_York") is None


async def test_rain_turned_off_sends_none_of_it(pool, store):
    await _site(store)
    await store.put(WeatherSettings(enabled=False))
    await _rain(pool, datetime.now(UTC) - timedelta(hours=3), 5.0)

    assert await chat.rainfall(pool, store, chat.WINDOW, "America/New_York") is None


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
        INSERT INTO pump_run (site_id, pump, started_at, ended_at, duration_s, role, started_by)
        VALUES (1, 1, $1::timestamptz, $1::timestamptz + interval '12 seconds', 12, 'lead', 'contact')
        """,
        evening,
    )

    numbers = await chat.facts(_app(pool, store), store)

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

    numbers = await chat.facts(_app(pool, store), store)
    body = chat.messages(store.chat, numbers)[1]["content"]

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
        INSERT INTO device_status (site_id, device, online, last_seen, last_error)
        VALUES (1, 'health0', false, now(), $1)
        ON CONFLICT (site_id, device) DO UPDATE SET online = false, last_seen = now(),
                                           last_error = excluded.last_error
        """,
        "[Errno 111] Connect call failed ('10.136.1.36', 1884)",
    )

    numbers = await chat.facts(_app(pool, store), store)
    body = chat.messages(store.chat, numbers)[1]["content"]

    assert numbers["devices"] == [{"device": "health0", "online": False, "last_seen": mock.ANY}]
    assert "10.136.1.36" not in body
    assert "Errno" not in body


# -- the scheduled one --------------------------------------------------------


# -- what it takes to be ready ------------------------------------------------


def test_a_key_is_needed_wherever_the_model_is():
    """Including on this network. llama.cpp speaks the OpenAI protocol and that
    includes the bearer token, so deciding that a private address needs no
    credential decides something about somebody's setup from the wrong side of
    it."""
    on_this_network = AiSettings(model="llama3", base_url="http://127.0.0.1:8080/v1", api_key="")
    assert not on_this_network.ready

    assert AiSettings(model="llama3", base_url="http://127.0.0.1:8080/v1", api_key="a-key").ready


def test_a_fresh_install_is_not_ready():
    """The model and the address are filled in by default and the key is the one
    thing nobody has typed."""
    assert not AiSettings().ready
    assert AiSettings().model and AiSettings().base_url


def test_the_instruction_says_which_window_it_is_reading():
    """It said "a week" while the payload said today, and the model did what a
    careful reader does with a contradiction: it spent its first paragraph
    explaining that it had been asked for a week and given a day."""
    for window in ("Today", "7 days", "30 days"):
        assert f"reading {window} of monitoring data" in chat.instructions(window)

    payload = chat.messages(ChatSettings(), {"window": "Today", "pumps": []})
    assert "reading Today of monitoring data" in payload[0]["content"]
    assert "over the week" not in payload[0]["content"]


def test_the_prompt_is_not_cut_off_mid_sentence():
    """Four thousand characters is about a page: enough for a paragraph about a
    pit and not enough for somebody describing a building, its history and the
    last three repairs."""
    long_one = "The pit is under the sidewalk. " * 400

    assert len(long_one) > 4000
    assert ChatSettings(description=long_one).description == long_one


# -- every call, not just the daily total -------------------------------------


async def _a_call(pool, when, pump=1, duration=12.0, both=False, high=False):
    cycle = await pool.fetchval(
        "INSERT INTO pump_cycle (site_id, started_at, first_pump, both_ran, high_water)"
        " VALUES (1, $1, $2, $3, $4) RETURNING id",
        when,
        pump,
        both,
        high,
    )
    await pool.execute(
        "INSERT INTO pump_run (site_id, cycle_id, pump, started_at, ended_at, duration_s,"
        " started_by) VALUES (1, $1, $2, $3, $4, $5, 'contact')",
        cycle,
        pump,
        when,
        when + timedelta(seconds=duration),
        duration,
    )


async def test_the_model_gets_one_line_per_call_with_the_gap_before_it(pool, store):
    """The whole reason the chat can answer "what happened on Friday at half
    past ten" where the old summary could only say "333 calls yesterday".

    The gap is computed here rather than left to the model to subtract. A list
    of timestamps is a list of timestamps, and the question is nearly always
    about the spacing.
    """
    await _site(store)
    now = datetime.now(UTC)
    await _a_call(pool, now - timedelta(minutes=40))
    await _a_call(pool, now - timedelta(minutes=37), pump=2)
    await _a_call(pool, now - timedelta(minutes=15), pump=1, both=True, high=True)

    lines = await chat.calls(pool, 1, chat.WINDOW, "America/New_York")

    assert len(lines) == 3
    assert lines[0]["gap_min"] is None, "the first has nothing before it to measure from"
    assert lines[1]["gap_min"] == 3.0
    assert lines[2]["gap_min"] == 22.0
    assert [line["pump"] for line in lines] == [1, 2, 1]
    assert lines[2]["both_pumps"] is True and lines[2]["high_water"] is True
    # On the building's clock, because every other time on the page is.
    assert lines[0]["at"][:2] == "20" and ":" in lines[0]["at"]


async def test_the_calls_stop_at_the_window(pool, store):
    await _site(store)
    now = datetime.now(UTC)
    await _a_call(pool, now - timedelta(hours=2))
    await _a_call(pool, now - timedelta(days=9))

    assert len(await chat.calls(pool, 1, chat.WINDOW, "UTC")) == 1


async def test_one_buildings_calls_are_not_anothers(pool, store):
    await _site(store)
    second = await pool.fetchval("INSERT INTO site (name) VALUES ('Next door') RETURNING id")
    now = datetime.now(UTC)
    await _a_call(pool, now - timedelta(minutes=10))
    await pool.execute(
        "INSERT INTO pump_cycle (site_id, started_at, first_pump) VALUES ($1, $2, 1)",
        second,
        now - timedelta(minutes=5),
    )

    assert len(await chat.calls(pool, 1, chat.WINDOW, "UTC")) == 1
    assert len(await chat.calls(pool, second, chat.WINDOW, "UTC")) == 1


# -- the transcript -----------------------------------------------------------


async def _a_person(pool, username: str) -> int:
    return await pool.fetchval(
        "INSERT INTO app_user (username, name, role, enabled, min_severity)"
        " VALUES ($1, $1, 'viewer', true, 'warning') RETURNING id",
        username,
    )


async def test_a_thread_comes_back_oldest_first(pool):
    who = await _a_person(pool, "david")
    for role, said in (("user", "how is it"), ("assistant", "fine"), ("user", "and now")):
        await chat.remember(pool, 1, who, role, said)

    thread = await chat.transcript(pool, 1, who)

    assert [line["content"] for line in thread] == ["how is it", "fine", "and now"]
    assert [line["role"] for line in thread] == ["user", "assistant", "user"]


async def test_your_questions_are_not_somebody_elses(pool):
    """Per person, which is the whole reason it is not one thread per building.
    A question is a half formed thought and nobody should have to ask theirs in
    public."""
    mine = await _a_person(pool, "david")
    theirs = await _a_person(pool, "ttsang")
    await chat.remember(pool, 1, mine, "user", "mine")
    await chat.remember(pool, 1, theirs, "user", "theirs")

    assert [line["content"] for line in await chat.transcript(pool, 1, mine)] == ["mine"]
    assert [line["content"] for line in await chat.transcript(pool, 1, theirs)] == ["theirs"]


async def test_a_thread_belongs_to_one_building_as_well_as_one_person(pool):
    who = await _a_person(pool, "david")
    second = await pool.fetchval("INSERT INTO site (name) VALUES ('Next door') RETURNING id")
    await chat.remember(pool, 1, who, "user", "about this pit")
    await chat.remember(pool, second, who, "user", "about the other one")

    here = await chat.transcript(pool, 1, who)
    there = await chat.transcript(pool, second, who)

    assert [line["content"] for line in here] == ["about this pit"]
    assert [line["content"] for line in there] == ["about the other one"]


async def test_starting_again_clears_only_your_own_thread_here(pool):
    mine = await _a_person(pool, "david")
    theirs = await _a_person(pool, "ttsang")
    second = await pool.fetchval("INSERT INTO site (name) VALUES ('Next door') RETURNING id")
    await chat.remember(pool, 1, mine, "user", "here")
    await chat.remember(pool, second, mine, "user", "next door")
    await chat.remember(pool, 1, theirs, "user", "not mine to clear")

    await chat.forget(pool, 1, mine)

    assert await chat.transcript(pool, 1, mine) == []
    assert len(await chat.transcript(pool, second, mine)) == 1
    assert len(await chat.transcript(pool, 1, theirs)) == 1


async def test_only_the_tail_of_a_long_thread_is_sent(pool):
    """A thread somebody has kept going for a month is mostly stale, and the
    readings ahead of it are rebuilt fresh every time anyway."""
    who = await _a_person(pool, "david")
    for n in range(chat.REMEMBER + 10):
        await chat.remember(pool, 1, who, "user", f"question {n}")

    thread = await chat.transcript(pool, 1, who)

    assert len(thread) == chat.REMEMBER
    assert thread[-1]["content"] == f"question {chat.REMEMBER + 9}", "the newest is kept"
    assert thread[0]["content"] == "question 10", "the oldest went"


async def test_both_halves_of_a_turn_are_written_down(pool, store, monkeypatch):
    """The question before the call and the answer after it, so a model that
    times out leaves the question in the thread rather than losing what
    somebody typed."""
    await _site(store)
    await store.put(AiSettings(api_key="sk-test", model="m"))
    who = await _a_person(pool, "david")

    async def answer(settings, payload):
        # The readings and the description go ahead of the conversation.
        assert payload[0]["role"] == "system"
        assert "readings" in payload[1]["content"]
        assert payload[-1]["content"] == "is it slowing down?"
        return "No, it is steady."

    monkeypatch.setattr(chat, "ask", answer)

    said = await chat.reply(_app(pool, store), store, who, "is it slowing down?")

    assert said == "No, it is steady."
    thread = await chat.transcript(pool, 1, who)
    assert [(line["role"], line["content"]) for line in thread] == [
        ("user", "is it slowing down?"),
        ("assistant", "No, it is steady."),
    ]


async def test_a_question_survives_a_model_that_refuses(pool, store, monkeypatch):
    await _site(store)
    await store.put(AiSettings(api_key="sk-test", model="m"))
    who = await _a_person(pool, "david")

    async def refuse(settings, payload):
        raise chat.ChatError("the model said no")

    monkeypatch.setattr(chat, "ask", refuse)

    with pytest.raises(chat.ChatError):
        await chat.reply(_app(pool, store), store, who, "what happened Friday?")

    thread = await chat.transcript(pool, 1, who)
    assert [line["content"] for line in thread] == ["what happened Friday?"]


async def test_the_settings_no_longer_carry_a_schedule():
    """They described when to write a paragraph unasked and who to mail it to.
    Both are gone, and a field left behind would be a setting that renders,
    saves and does nothing."""
    fresh = ChatSettings()

    assert fresh.description == ""
    for gone in ("schedule", "schedule_at", "schedule_window", "notify", "scheduled"):
        assert not hasattr(fresh, gone), gone


# -- the whole request has a budget ------------------------------------------


def _numbers(window="the last 7 days"):
    return {"window": window, "pumps": [{"pump": 1, "runs_this_week": 12}]}


def _calls(n):
    return [
        {
            "at": f"2026-09-{1 + i % 28:02d} 04:05:06",
            "gap_min": 3.2,
            "pump": 1 + i % 2,
            "ran_s": 12.4,
            "both_pumps": False,
            "high_water": False,
        }
        for i in range(n)
    ]


HEADER = "at,gap_min,pump,ran_s,both_pumps,high_water"


def _cost(payload):
    """Counted the way the budget counts it, message by message: the table is
    dense and everything else is prose. A single ratio for both is what made
    the budget wrong by four times."""
    total = 0
    for part in payload:
        head, found, rows = part["content"].partition(HEADER)
        total += chat.tokens(head)
        if found:
            total += chat.tokens(found + rows, dense=True)
    return total


def test_a_busy_pit_does_not_blow_the_window():
    """Twenty thousand calls is 165,000 tokens as a table, and the model refuses
    the request outright rather than answering a shorter version of it. This is
    the bug that shipped: the first question ever asked came back
    "160,001 input tokens"."""
    payload = chat.messages(ChatSettings(), _numbers(), [], _calls(20_000), budget=100_000)

    assert _cost(payload) <= 100_000
    # And it says what it left out, so the model does not read the first listed
    # call as the first call there was.
    assert "Only the most recent" in payload[1]["content"]
    assert "cover the whole window" in payload[1]["content"]


def test_the_newest_calls_are_the_ones_kept():
    """A question is far more often about this week than about the first week of
    the window."""
    rows = _calls(20_000)
    payload = chat.messages(ChatSettings(), _numbers(), [], rows, budget=60_000)

    listed = payload[1]["content"]
    assert rows[-1]["at"] in listed, "the newest call is there"
    assert listed.count("\n2026-") < len(rows)


def test_a_long_conversation_is_trimmed_before_the_readings_are():
    """The readings are what the questions are about and are rebuilt fresh every
    time. The thread is the part that grows without anybody deciding to."""
    # Sixty turns of five thousand tokens each: three hundred thousand, against
    # a hundred thousand budget, so something has to give.
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "x" * 20_000}
        for i in range(60)
    ]
    payload = chat.messages(ChatSettings(), _numbers(), history, _calls(200), budget=100_000)

    assert _cost(payload) <= 100_000
    # The readings survived whole.
    assert "runs_this_week" in payload[1]["content"]
    assert "2026-" in payload[1]["content"], "the calls are still there"
    # And what is left of the thread is the end of it, not the beginning.
    kept = payload[2:]
    assert 0 < len(kept) < len(history)
    assert kept[-1]["content"].startswith("turn 59"), "the newest turn is kept"
    assert not kept[0]["content"].startswith("turn 0"), "the oldest went"


def test_everything_fits_when_there_is_room():
    """The budget is a ceiling, not a haircut. A week on a quiet pit goes in
    whole, conversation and all."""
    history = [{"role": "user", "content": "how is it?"}]
    rows = _calls(300)
    payload = chat.messages(ChatSettings(description="Two pumps."), _numbers(), history, rows)

    assert len(payload) == 3, "system, readings, and the one turn"
    assert "Only the most recent" not in payload[1]["content"], "nothing was dropped"
    assert rows[0]["at"] in payload[1]["content"], "including the oldest call"
    assert "Two pumps." in payload[1]["content"]


def test_a_description_and_the_rollups_are_never_cut():
    """They are small, and they are the difference between an answer and a
    shrug. Everything else gives way first."""
    payload = chat.messages(
        ChatSettings(description="A check valve was replaced in the spring."),
        _numbers(),
        [{"role": "user", "content": "y" * 400_000}],
        _calls(20_000),
        budget=100_000,
    )

    assert "A check valve was replaced in the spring." in payload[1]["content"]
    assert "runs_this_week" in payload[1]["content"]
    assert _cost(payload) <= 100_000


async def test_an_unanswered_question_is_kept_but_not_sent_back(pool, store, monkeypatch):
    """It stays on the page so nobody loses what they typed, and it is left out
    of the request so the model is not handed two user turns in a row.

    Observed doing real harm: four unanswered questions had piled up during
    testing and the next answer opened "with no telemetry supplied here, I
    can't determine how the pit behaved", with twenty three thousand tokens of
    telemetry directly above them.
    """
    await _site(store)
    await store.put(AiSettings(api_key="sk-test", model="m"))
    who = await _a_person(pool, "david")

    # One that failed, and is therefore unanswered.
    await chat.remember(pool, 1, who, "user", "what happened Friday?")

    seen = {}

    async def answer(settings, payload):
        seen["payload"] = payload
        return "Nothing unusual."

    monkeypatch.setattr(chat, "ask", answer)
    await chat.reply(_app(pool, store), store, who, "and Saturday?")

    whole = "\n".join(part["content"] for part in seen["payload"])
    assert "what happened Friday?" not in whole, "the unanswered one was sent back"
    assert seen["payload"][-1]["content"] == "and Saturday?"
    # The readings are themselves a user message, so the last two turns being
    # user is the shape by design. What must never happen is a run of
    # unanswered questions inside the conversation that follows them.
    thread_part = seen["payload"][2:]
    roles = [part["role"] for part in thread_part]
    assert not any(a == b == "user" for a, b in pairwise(roles))

    # And it is still on the page.
    thread = await chat.transcript(pool, 1, who)
    assert next(line["content"] for line in thread) == "what happened Friday?"


def test_only_complete_pairs_go_back():
    thread = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "answered"},
        {"role": "user", "content": "two, never answered"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "also answered"},
        {"role": "user", "content": "four, still in flight"},
    ]

    assert [line["content"] for line in chat.paired(thread)] == [
        "one",
        "answered",
        "three",
        "also answered",
    ]


def test_a_table_of_numbers_is_counted_four_times_heavier_than_prose():
    """Measured against qwen3.8-27b on 2026-09-19: 6,180 characters of English
    counted as 1,252 tokens, and 79,199 characters of the calls table counted
    as 76,851. Treating both as four characters to a token is how the budget
    came to believe a 77,000 token request was 23,000."""
    rows = chat.table(_calls(2400))

    assert 0.9 < len(rows) / chat.tokens(rows, dense=True) < 1.2
    assert 3.5 < len("word " * 2000) / chat.tokens("word " * 2000) < 4.5


def test_the_budget_is_kept_in_the_units_the_model_counts_in():
    """The table is what fills a request, so counting it wrong is counting the
    request wrong."""
    payload = chat.messages(ChatSettings(), _numbers(), [], _calls(40_000), budget=100_000)

    assert _cost(payload) <= 100_000
    # And in characters that is about 100k too, not 400k, because the table is
    # where the characters are and it is counted at one to one.
    assert len("".join(part["content"] for part in payload)) < 130_000
