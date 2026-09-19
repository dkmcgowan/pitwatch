"""The written summary: what the numbers are, and what a model made of them.

Two halves that stay apart on purpose. Everything up to `facts` is arithmetic
this application can defend, and it is stored beside whatever comes back, so a
summary read a month later can be checked against what it was actually looking
at. Only the last step leaves the building.

What is sent: the description somebody wrote on the settings page, and a page
of numbers. Not the site name, not the address, not a single account name.
Nobody needs a street address to say whether a pump is drawing more than it
did last week, and the one thing the owner of this pit has been clear about is
that his address is not public. The rain goes with it as a daily total and
never as a coordinate, which is the same rule: the number is the finding and
the place is not.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx2

from pitwatch import clock, domain
from pitwatch.domain import series
from pitwatch.domain import tides as tide_domain
from pitwatch.domain import weather as weather_domain
from pitwatch.domain.history import CurrentHistory
from pitwatch.schemas import AiSettings, ChatSettings
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

# Long enough for a slow model on a busy afternoon, short enough that a browser
# waiting on it has not given up first.
#
# Raised from 90 when the chat replaced the one shot summary. A summary was one
# question against two thousand tokens; a chat is an arbitrary question against
# twenty three thousand, and "has the pit been quieter in the afternoons than at
# night" is a different amount of work from "summarize the week". Measured on a
# local qwen3.8-27b: the easy ones came back in thirty to fifty seconds and that
# one did not come back in ninety.
TIMEOUT_S = 240.0

# The window a check reads when nobody has chosen one. A week is long enough to
# have a shape and short enough that a change in it is recent.
WINDOW = series.WINDOWS["7d"]


# What the whole request is allowed to cost, in tokens.
#
# Not a cap on any one part. The first version of this capped the number of
# calls, at six thousand, which was a number standing in for a token budget and
# fooling nobody: it said nothing about the conversation, which grows without
# limit, and nothing about a description somebody has written three pages of.
# Counting the whole request is the only cap that means anything.
#
# 100,000 against this model's 160,000 leaves room for the answer and for the
# estimate below being wrong by a third.
BUDGET_TOKENS = 100_000

# Characters per token, and the two numbers are nothing like each other.
#
# "Roughly four characters to a token" is true of English and badly wrong for a
# table of timestamps and decimals, where a tokenizer gives up and spends a
# token on almost every character. Measured against qwen3.8-27b on 2026-09-19:
# 6,180 characters of prose came back counted as 1,252 tokens, which is 4.94 to
# one; 79,199 characters of the calls table came back as 76,851, which is 1.03.
#
# Using four for both is how the budget came to believe a request was 23,000
# tokens when the model counted 77,000. It fitted anyway, so nothing broke and
# nothing said so, which is the worst way for a number to be wrong.
PER_TOKEN_PROSE = 4
PER_TOKEN_DENSE = 1

# How much of what is left the conversation may take, once the readings are in.
#
# The readings come first because they are what the questions are about and are
# rebuilt fresh every time. But a chat that forgets the last thing it said is
# not a chat, so the recent turns are reserved before the calls are poured in.
HISTORY_SHARE = 0.25


# Held back from the calls' share for the prose around the table: the sentence
# describing the columns, and the one saying how many calls were left out.
ABOUT_THE_TABLE = 200


def tokens(text: str, dense: bool = False) -> int:
    """About how many tokens that is.

    `dense` for a table of numbers and timestamps, which costs about four times
    what the same length of prose costs. See PER_TOKEN_DENSE.
    """
    return len(text) // (PER_TOKEN_DENSE if dense else PER_TOKEN_PROSE)


# Said in one place, because the page draws it and the post refuses with it.
NOT_READY = "Add an API key and a model on the settings page first."

# There was a gate here, holding the button until a week had passed or the
# description had changed, on the reasoning that the same readings and the same
# words give the same answer for money. It went on 2026-09-09, when the base URL
# turned out to be the setting that mattered: an installation answering itself
# on its own network has no per call cost to ration, and rationing a free thing
# is a page arguing with somebody about their own hardware.

# Every daily figure here is cut on the site's own midnight, the meter's and
# the panel's and the rain's alike. Days that do not start at the same moment
# cannot be read down the page against each other, and reading them against
# each other is the whole reason a week is sent as days rather than as a total.
DAILY = """
SELECT time_bucket('1 day', ts, timezone => $4::text)  AS day,
       max(current)                                    AS peak,
       avg(current) FILTER (WHERE current >= $3)       AS running_mean,
       count(*)     FILTER (WHERE current >= $3)       AS running_samples
FROM em_sample
WHERE site_id = $5 AND channel = $1 AND ts > now() - $2::interval
GROUP BY 1
ORDER BY 1
"""

# Runs and how long they lasted, per day, from the panel's own run contacts.
# The count used to come from the meter, by looking for the current rising off
# nothing, and that undercounts: two runs close together arrive from the meter
# as one. Handing a model an undercount and asking it whether anything has
# changed is asking it to explain an artifact.
DAILY_RUNS = """
SELECT time_bucket('1 day', started_at, timezone => $3::text) AS day,
       count(*)                          AS runs,
       round(avg(duration_s)::numeric, 1) AS mean_duration_s,
       round(max(duration_s)::numeric, 1) AS longest_s
FROM pump_run
WHERE site_id = $4 AND pump = $1 AND started_at > now() - $2::interval
GROUP BY 1
ORDER BY 1
"""

# The week's calls for water, which is the closest thing here to a measurement
# of what is coming in.
CALLS = """
SELECT count(*)                             AS calls,
       count(*) FILTER (WHERE both_ran)     AS both_ran,
       count(*) FILTER (WHERE high_water)   AS high_water,
       round(percentile_cont(0.5) WITHIN GROUP (
           ORDER BY extract(epoch FROM started_at - previous)
       )::numeric, 0)                       AS typical_gap_s
FROM (
    SELECT started_at, both_ran, high_water,
           lag(started_at) OVER (ORDER BY started_at) AS previous
    FROM pump_cycle
    WHERE site_id = $2 AND started_at > now() - $1::interval
) spaced
"""


def instructions(window: str) -> str:
    """What to do with the numbers, and over what.

    The window is written into the sentence rather than assumed. It said "a
    week" while the payload said today, and the model did what a careful reader
    does with a contradiction: it spent its first paragraph explaining that it
    had been asked for a week and given a day. The instruction and the data have
    to agree about what is being read.
    """
    return (
        f"You are reading {window} of monitoring data from a duplex ejector "
        "pump panel in a building, and answering questions about it for the "
        "person responsible for keeping it running. Where no question has been "
        "asked yet, say whether the system looks healthy, what changed over "
        "that period, and anything worth watching or acting on. Be specific "
        "and use "
        "the numbers. Where the data is too thin to support a conclusion, say "
        "so plainly rather than hedging. Never invent a reading that is not in "
        "the data. Where rainfall is given, read the pit against it: a busy "
        "spell in two inches of rain and a busy spell in a dry one are "
        "different findings. Where tide is given, read it against that too: a "
        "pit near tidal water fills with the water table, so a rise in calls "
        "that tracks high water is groundwater rather than anything in the "
        "building. Answer the question that was asked, at the length it "
        "deserves: one line for a question with a one line answer, four short "
        "paragraphs at most for an open one.\n\n"
        "Write plain text. No markdown of any kind: no asterisks for emphasis, "
        "no headings, no bullet points, no backticks. What you write is "
        "rendered as the characters you send, because output from a model is "
        "not markup and will not be treated as any, so a pair of asterisks "
        "reaches the reader as a pair of asterisks."
    )


async def rainfall(pool, store: SettingsStore, window: series.Window, zone: str) -> dict | None:
    """The window's rain, by day, in whatever unit the site reads.

    The one thing in here from outside the building, and the reason a busy week
    is worth anything: a pit that called forty times in a dry week and a pit
    that called forty times in two inches of rain are two different pits. Asked
    for over the same days as everything else and cut on the same midnight, so
    a wet day and a busy day are the same twenty four hours.

    None when there is no rain to send, which is a fresh install, an
    installation with no coordinates, or one that turned this off. That is not
    the same as a dry week and the model is told which it has.
    """
    if not store.weather.enabled or not store.site.has_coordinates:
        return None
    # A day at a time whatever the window is. The window's own bucket is an hour
    # on today, which would put twenty four rows on the page under the same date
    # and hand the model a column it cannot read.
    daily = await weather_domain.rain_series(pool, window.span, timedelta(days=1), zone)
    if not daily:
        return None
    units = store.weather.units
    return {
        "units": units,
        # Said plainly, because a model handed a rainfall column will otherwise
        # write about it as though somebody read a gauge.
        "source": "hourly model output for this location, not a rain gauge",
        "days": [
            {"day": when.date().isoformat(), "rain": weather_domain.as_read(mm, units)}
            for when, mm in daily
        ],
    }


async def tide(pool, store: SettingsStore, window: series.Window, zone: str) -> dict | None:
    """High and low water per day, in whatever unit the site reads.

    The other half of what is outside the building. Rain is the water that fell
    on it; this is the water table it sits in, and on tidal ground the second
    one moves the pit far more than the first. A model handed a busy week with
    no tide column will attribute it to the building.

    None when there is no station, which is most installations: a pit in the
    middle of a county has no tide and should not be sent a column of nulls.
    """
    if not store.tide.ready:
        return None
    days = await tide_domain.daily(pool, window.span, zone)
    if not days:
        return None
    units = store.tide.units
    return {
        "units": units,
        "station": store.tide.station_name or store.tide.station,
        # Said out loud, because a model handed feet of water will otherwise
        # write about it as though somebody measured a level in the pit.
        "source": "the published tide gauge for this location, not a level in the pit",
        "days": [
            {
                "day": day["day"],
                "high_water": tide_domain.as_read(day["high_water"], units),
                "low_water": tide_domain.as_read(day["low_water"], units),
                "most_above_prediction": tide_domain.as_read(day["most_above_prediction"], units),
            }
            for day in days
        ],
    }


async def facts(app, store: SettingsStore, window: series.Window = WINDOW) -> dict:
    """The week in numbers, in the shape the model is given it.

    Deliberately small. A week of raw readings is tens of thousands of rows and
    says nothing a daily figure does not; the point of this is to be checkable
    by somebody reading it later, not to be exhaustive.
    """
    pool: asyncpg.Pool = app.state.pool
    clamp = store.mqtt.clamp_for_pump
    history: CurrentHistory | None = getattr(app.state, "history", None)
    zone = store.site.timezone

    pumps = []
    for number, pump in store.pumps.by_number.items():
        channel = clamp[number]
        try:
            rows = await pool.fetch(
                DAILY, channel, window.span, domain.RUNNING_AMPS, zone, store.site_id
            )
            run_rows = await pool.fetch(DAILY_RUNS, number, window.span, zone, store.site_id)
        except (asyncpg.PostgresError, OSError) as error:
            log.warning("Could not read the daily figures: %s", error)
            rows, run_rows = [], []

        by_day = {
            row["day"].date().isoformat(): {
                "runs": int(row["runs"]),
                "mean_run_seconds": float(row["mean_duration_s"] or 0.0),
                "longest_run_seconds": float(row["longest_s"] or 0.0),
            }
            for row in run_rows
        }
        days = []
        for row in rows:
            day = row["day"].date().isoformat()
            days.append(
                {
                    "day": day,
                    "peak_amps": round(float(row["peak"] or 0.0), 2),
                    "running_amps": (
                        round(float(row["running_mean"]), 2) if row["running_mean"] else None
                    ),
                    "readings_while_running": int(row["running_samples"] or 0),
                    **by_day.pop(day, {"runs": 0}),
                }
            )
        # A day the pump ran on but the meter said nothing about is a real day
        # on a pit with one CT fitted, so it goes in with what is known.
        for day, counted in by_day.items():
            days.append({"day": day, **counted})

        typical = None
        if history is not None:
            measured = await history.typical(pool, store.site_id, channel, domain.RUNNING_AMPS)
            if measured.median is not None:
                typical = {
                    "this_week_amps": round(measured.median, 2),
                    "four_weeks_before_amps": (
                        round(measured.earlier_median, 2)
                        if measured.earlier_median is not None
                        else None
                    ),
                    "readings": measured.samples,
                }

        pumps.append(
            {
                "pump": number,
                "name": pump.name or f"Pump {number}",
                "runs_this_week": sum(day.get("runs", 0) for day in days),
                "typical_load": typical,
                "days": sorted(days, key=lambda entry: entry["day"]),
            }
        )

    try:
        called = await pool.fetchrow(CALLS, window.span, store.site_id)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the week's calls: %s", error)
        called = None
    calls = {
        "calls_this_week": int(called["calls"]) if called else 0,
        "both_pumps_ran": int(called["both_ran"]) if called else 0,
        "reached_high_float": int(called["high_water"]) if called else 0,
        "typical_seconds_between_calls": (
            float(called["typical_gap_s"]) if called and called["typical_gap_s"] else None
        ),
    }

    inputs = store.mqtt
    assigned = list(inputs.used_channels)
    spans = await series.contact_spans(
        pool, store.site_id, [mapped.channel for mapped in assigned], window
    )
    contacts = [
        {
            "name": mapped.title,
            "closed_this_week": len(spans.get(mapped.channel, [])),
            "last_closed": (
                spans[mapped.channel][-1][0].isoformat() if spans.get(mapped.channel) else None
            ),
        }
        for mapped in assigned
    ]

    # Online and when, and not the error text. `last_error` is a connection
    # failure written by the client, so it carries broker addresses and library
    # wording, and this summary is about pumps, amps and contacts. What it is
    # worth to a reader of the summary is that a device was not answering, and
    # `online` says that. The full text is on the Diagnostics page, where it is
    # for the person fixing it.
    devices = []
    try:
        rows = await pool.fetch(
            "SELECT device, online, last_seen FROM device_status WHERE site_id = $1",
            store.site_id,
        )
    except (asyncpg.PostgresError, OSError):
        rows = []
    for row in rows:
        devices.append(
            {
                "device": row["device"],
                "online": row["online"],
                "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
            }
        )

    return {
        "window": window.title,
        "generated_at": datetime.now(UTC).isoformat(),
        "running_threshold_amps": domain.RUNNING_AMPS,
        "rain": await rainfall(pool, store, window, zone),
        "tide": await tide(pool, store, window, zone),
        "calls_for_water": calls,
        "pumps": pumps,
        "panel_contacts": contacts,
        "devices": devices,
        # Said out loud, because a model handed a page of zeroes will otherwise
        # explain what the zeroes mean rather than that nothing is wired.
        "panel_module_connected": bool(inputs.enabled and inputs.host),
    }


def messages(
    settings: ChatSettings,
    numbers: dict,
    history: list[dict] | None = None,
    every_call: list[dict] | None = None,
    budget: int = BUDGET_TOKENS,
) -> list[dict]:
    """The whole request: what to be, what is true, and what has been said.

    The readings go in as one message ahead of the conversation rather than
    being folded into the system prompt, so that a long thread still has the
    numbers in front of it and the model is not answering question eleven from
    memory of question one.

    Rebuilt on every request, never stored. A stored prompt freezes a
    description somebody has since corrected and freezes the readings to
    whatever they were that afternoon, and both would then quietly be wrong.

    **Everything here is fitted to a budget.** Three of the four parts grow
    without a natural limit: a description somebody keeps adding to, a
    conversation somebody keeps going, and a window of calls on a busy pit. The
    first version of this sent them all and was refused by the model at 160,001
    tokens on the very first question. What cannot be cut is the system prompt,
    the description and the per-day rollups; those are small and they are the
    difference between an answer and a shrug. What gets cut, in order, is the
    oldest turns of the conversation and then the oldest calls.
    """
    described = settings.description.strip() or (
        "No description of the system has been written on the settings page."
    )
    system = instructions(numbers.get("window") or "a week")
    preamble = (
        "This is the system, described by the person who looks after it:\n\n"
        f"{described}\n\n"
        "These are the readings:\n\n"
        f"{json.dumps(numbers, indent=1, sort_keys=True)}"
    )

    left = budget - tokens(system) - tokens(preamble)

    # The conversation first, newest backwards, up to its share of what is
    # left. A chat that has forgotten the last thing it said is not a chat.
    kept: list[dict] = []
    for turn in reversed(history or []):
        cost = tokens(turn["content"])
        if cost > left * HISTORY_SHARE:
            break
        kept.insert(0, turn)
        left -= cost

    # Then the calls, newest backwards, into whatever is left. ABOUT_THE_TABLE
    # is held back for the two paragraphs that explain the columns and say what
    # was left out, which are written after the fitting and would otherwise
    # push the request over the line it was just fitted to.
    listed, dropped = _calls_that_fit(every_call or [], left - ABOUT_THE_TABLE)
    if listed:
        preamble += (
            "\n\nEvery call for water in the window, one per line. "
            "`gap_min` is the minutes since the call before it, blank on the "
            "first; `ran_s` is how long the pump ran; `both_pumps` and "
            "`high_water` are 1 when true and blank otherwise."
        )
        if dropped:
            # Said out loud. Without it a model reads the first listed call as
            # the first call there was and reports that the pit sat idle for
            # the earlier half of the window.
            preamble += (
                f"\n\nOnly the most recent {len(listed)} of {len(listed) + dropped} "
                "calls are listed, to stay inside the request size. The per-day "
                "figures above cover the whole window."
            )
        preamble += f"\n\n{table(listed)}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": preamble},
        *kept,
    ]


def _calls_that_fit(rows: list[dict], budget: int) -> tuple[list[dict], int]:
    """As many of the newest calls as the budget allows, and how many went.

    Measured by rendering rather than by estimating a cost per row. An estimate
    is what put the first version 3,212 tokens over its own budget: the header
    line skews a per-row average, and the sentence explaining the truncation is
    written after the arithmetic that decided on it. Rendering the candidate
    and asking how big it is cannot be wrong in that way, and it costs a few
    string joins on a request that is about to wait thirty seconds for a model.
    """
    if not rows or budget <= 0:
        return [], len(rows)

    keep = len(rows)
    while keep and tokens(table(rows[-keep:]), dense=True) > budget:
        # Ten percent at a time down to the last few, which converges in about
        # forty passes from twenty thousand rows.
        keep = keep * 9 // 10 if keep > 10 else keep - 1
    return rows[-keep:], len(rows) - keep


class ChatError(RuntimeError):
    """Something a person can act on, ready to put on the page."""


async def ask(settings: AiSettings, payload: list[dict]) -> str:
    """One call, and whatever it says back.

    Nothing but the model and the messages is sent. Every other knob has been
    renamed or restricted by one model family or another, and a summary that
    fails because a temperature was attached to a model that does not take one
    is a summary that fails for no reason.
    """
    if not settings.ready:
        raise ChatError(NOT_READY)

    url = settings.base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                json={"model": settings.model, "messages": payload},
            )
    except httpx2.TimeoutException as error:
        # Said as a timeout rather than as "could not reach", which is what it
        # used to say with an empty reason after it: httpx gives a read timeout
        # no message at all, so the page showed a colon and then nothing.
        raise ChatError(
            f"The model at {url} did not answer within {TIMEOUT_S:.0f} seconds. "
            "The question is still in the conversation; ask again, or ask a "
            "narrower one."
        ) from error
    except httpx2.HTTPError as error:
        raise ChatError(f"Could not reach {url}: {error}") from error

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code >= 400:
        said = ""
        if isinstance(body.get("error"), dict):
            said = str(body["error"].get("message") or "")
        raise ChatError(said or f"{url} answered {response.status_code}.")

    try:
        written = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ChatError("The reply did not contain a summary.") from None

    written = (written or "").strip()
    if not written:
        raise ChatError("The model returned nothing.")
    return written


# One line per call for water, on top of the daily rollups.
#
# Measured against a month of real data: 2,461 calls is about twelve thousand
# tokens, next to two thousand for the rollups alone. Nine percent of a 150k
# window buys the difference between a model that can say "333 calls yesterday"
# and one that can answer "what happened on Friday at half past ten", which is
# the question people actually arrive with.
#
# Amp readings are deliberately not here. There are 137,000 of them in a month,
# about 690,000 tokens, and they say nothing per-reading that the per-run peak
# and steady figures do not say already.
EVERY_CALL = """
SELECT c.started_at,
       round(extract(epoch FROM c.started_at
             - lag(c.started_at) OVER (ORDER BY c.started_at))) AS gap_s,
       c.both_ran,
       c.high_water,
       r.pump,
       round(r.duration_s::numeric, 1) AS ran_s
FROM pump_cycle c
LEFT JOIN LATERAL (
    SELECT pump, duration_s FROM pump_run
    WHERE cycle_id = c.id ORDER BY started_at LIMIT 1
) r ON true
WHERE c.site_id = $1 AND c.started_at > now() - $2::interval
ORDER BY c.started_at
"""


def table(rows: list[dict]) -> str:
    """The calls as a table, not as a list of objects.

    Measured, because the first version of this shipped as JSON and blew the
    model's context window on the first question: 2,453 calls came to 80,000
    tokens as one object per call, 63,000 compact, and 20,000 as this. A key
    repeated 2,453 times is 2,453 copies of the key.
    """
    lines = ["at,gap_min,pump,ran_s,both_pumps,high_water"]
    for row in rows:
        lines.append(
            "{},{},{},{},{},{}".format(
                row["at"],
                "" if row["gap_min"] is None else row["gap_min"],
                row["pump"] or "",
                "" if row["ran_s"] is None else row["ran_s"],
                1 if row["both_pumps"] else "",
                1 if row["high_water"] else "",
            )
        )
    return "\n".join(lines)


async def calls(pool: asyncpg.Pool, site_id: int, window: series.Window, zone: str) -> list[dict]:
    """Every call for water in the window, with the gap before it.

    The gap is the useful column and it is computed here rather than left to
    the model to subtract: a list of timestamps is a list of timestamps, and
    the question is nearly always about the spacing.
    """
    try:
        rows = await pool.fetch(EVERY_CALL, site_id, window.span)
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the calls for the chat: %s", error)
        return []
    return [
        {
            "at": clock.local(row["started_at"], zone).strftime("%Y-%m-%d %H:%M:%S"),
            "gap_min": (None if row["gap_s"] is None else round(float(row["gap_s"]) / 60.0, 1)),
            "pump": row["pump"],
            "ran_s": None if row["ran_s"] is None else float(row["ran_s"]),
            "both_pumps": row["both_ran"],
            "high_water": row["high_water"],
        }
        for row in rows
    ]


# -- the transcript ----------------------------------------------------------
#
# One thread per person per building. Appended to and read back in order, and
# never edited: it is a record of what was asked and what came back.

# How much of a thread is sent back to the model.
#
# Not the whole of it forever. A thread somebody has kept going for a month is
# mostly stale, the readings ahead of it are rebuilt fresh every time anyway,
# and the tail is what the next answer depends on. Pairs, so a reply is never
# sent without the question it answered.
REMEMBER = 40


async def transcript(
    pool: asyncpg.Pool, site_id: int, user_id: int, limit: int = REMEMBER
) -> list[dict]:
    """The tail of this person's thread in this building, oldest first."""
    rows = await pool.fetch(
        """
        SELECT role, content, created_at FROM (
            SELECT id, role, content, created_at FROM chat_message
            WHERE site_id = $1 AND user_id = $2
            ORDER BY created_at DESC, id DESC LIMIT $3
        ) tail ORDER BY created_at, id
        """,
        site_id,
        user_id,
        limit,
    )
    return [
        {"role": row["role"], "content": row["content"], "at": row["created_at"]} for row in rows
    ]


def paired(thread: list[dict]) -> list[dict]:
    """Only the turns that were answered.

    A question whose call failed stays in the thread on purpose, so nobody
    loses what they typed. It must not be sent back to the model, though: two
    user turns in a row is a malformed conversation, and it was observed doing
    real harm. Four unanswered questions had piled up during testing and the
    next answer opened "with no telemetry supplied here, I can't determine how
    the pit behaved" -- with twenty three thousand tokens of telemetry sitting
    directly above them.

    So what goes back is complete pairs, and the unanswered ones stay on the
    page where the person who typed them can see them.
    """
    kept: list[dict] = []
    for i, line in enumerate(thread):
        following = thread[i + 1] if i + 1 < len(thread) else None
        if line["role"] == "user" and (following is None or following["role"] != "assistant"):
            continue
        kept.append(line)
    return kept


async def remember(pool: asyncpg.Pool, site_id: int, user_id: int, role: str, content: str) -> None:
    await pool.execute(
        "INSERT INTO chat_message (site_id, user_id, role, content) VALUES ($1, $2, $3, $4)",
        site_id,
        user_id,
        role,
        content,
    )


async def forget(pool: asyncpg.Pool, site_id: int, user_id: int) -> None:
    """Start again. Only ever this person's thread in this building."""
    await pool.execute(
        "DELETE FROM chat_message WHERE site_id = $1 AND user_id = $2", site_id, user_id
    )


async def reply(app, store: SettingsStore, user_id: int, asked: str, window=None) -> str:
    """Ask the model, with the readings and the thread, and keep both sides.

    The question is written down before the call and the answer after it, so a
    model that times out leaves the question in the thread rather than losing
    what somebody typed.
    """
    pool: asyncpg.Pool = app.state.pool
    site_id = store.site_id
    window = window or WINDOW

    numbers = await facts(app, store, window)
    every_call = await calls(pool, site_id, window, store.site.timezone)

    before = await transcript(pool, site_id, user_id)
    await remember(pool, site_id, user_id, "user", asked)

    said = await ask(
        store.ai,
        messages(
            store.chat,
            numbers,
            [
                *paired([{"role": row["role"], "content": row["content"]} for row in before]),
                {"role": "user", "content": asked},
            ],
            every_call,
        ),
    )
    await remember(pool, site_id, user_id, "assistant", said)
    log.info("Chat reply for site %s user %s over %s", site_id, user_id, window.title)
    return said


__all__ = [
    "NOT_READY",
    "REMEMBER",
    "ChatError",
    "ask",
    "calls",
    "facts",
    "forget",
    "instructions",
    "messages",
    "paired",
    "rainfall",
    "remember",
    "reply",
    "table",
    "tide",
    "tokens",
    "transcript",
]
