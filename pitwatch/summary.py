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
from datetime import UTC, datetime

import asyncpg
import httpx2

from pitwatch import domain
from pitwatch.domain import series
from pitwatch.domain import weather as weather_domain
from pitwatch.domain.history import CurrentHistory
from pitwatch.schemas import SummarySettings
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

# Long enough for a slow model on a busy afternoon, short enough that a browser
# waiting on it has not given up first.
TIMEOUT_S = 90.0

WINDOW = series.WINDOWS["7d"]

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
WHERE channel = $1 AND ts > now() - $2::interval
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
WHERE pump = $1 AND started_at > now() - $2::interval
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
    WHERE started_at > now() - $1::interval
) spaced
"""

INSTRUCTIONS = (
    "You are reading a week of monitoring data from a duplex ejector pump "
    "panel in a building, for the person responsible for keeping it running. "
    "Say whether the system looks healthy, what changed over the week, and "
    "anything worth watching or acting on. Be specific and use the numbers. "
    "Where the data is too thin to support a conclusion, say so plainly "
    "rather than hedging. Never invent a reading that is not in the data. "
    "Where rainfall is given, read the pit against it: a busy week in two "
    "inches of rain and a busy week in a dry one are different findings. "
    "Four short paragraphs at most, plain text, no headings and no bullet "
    "points."
)


async def rainfall(pool, store: SettingsStore, window: series.Window, zone: str) -> dict | None:
    """The week's rain, by day, in whatever unit the site reads.

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
    daily = await weather_domain.rain_series(pool, window.span, window.count_bucket, zone)
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


async def facts(app, window: series.Window = WINDOW) -> dict:
    """The week in numbers, in the shape the model is given it.

    Deliberately small. A week of raw readings is tens of thousands of rows and
    says nothing a daily figure does not; the point of this is to be checkable
    by somebody reading it later, not to be exhaustive.
    """
    store: SettingsStore = app.state.settings
    pool: asyncpg.Pool = app.state.pool
    clamp = store.mqtt.clamp_for_pump
    history: CurrentHistory | None = getattr(app.state, "history", None)
    zone = store.site.timezone

    pumps = []
    for number, pump in store.pumps.by_number.items():
        channel = clamp[number]
        try:
            rows = await pool.fetch(DAILY, channel, window.span, domain.RUNNING_AMPS, zone)
            run_rows = await pool.fetch(DAILY_RUNS, number, window.span, zone)
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
            measured = await history.typical(pool, channel, domain.RUNNING_AMPS)
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
        called = await pool.fetchrow(CALLS, window.span)
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
    spans = await series.contact_spans(pool, [mapped.channel for mapped in assigned], window)
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
        rows = await pool.fetch("SELECT device, online, last_seen FROM device_status")
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
        "calls_for_water": calls,
        "pumps": pumps,
        "panel_contacts": contacts,
        "devices": devices,
        # Said out loud, because a model handed a page of zeroes will otherwise
        # explain what the zeroes mean rather than that nothing is wired.
        "panel_module_connected": bool(inputs.enabled and inputs.host),
    }


def messages(settings: SummarySettings, numbers: dict) -> list[dict]:
    described = settings.description.strip() or (
        "No description of the system has been written on the settings page."
    )
    return [
        {"role": "system", "content": INSTRUCTIONS},
        {
            "role": "user",
            "content": (
                "This is the system, described by the person who looks after it:\n\n"
                f"{described}\n\n"
                "These are the readings:\n\n"
                f"{json.dumps(numbers, indent=1, sort_keys=True)}"
            ),
        },
    ]


class SummaryError(RuntimeError):
    """Something a person can act on, ready to put on the page."""


async def ask(settings: SummarySettings, payload: list[dict]) -> str:
    """One call, and whatever it says back.

    Nothing but the model and the messages is sent. Every other knob has been
    renamed or restricted by one model family or another, and a summary that
    fails because a temperature was attached to a model that does not take one
    is a summary that fails for no reason.
    """
    if not settings.ready:
        raise SummaryError("Add an OpenAI key and a model on the settings page first.")

    url = settings.base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.api_key}"},
                json={"model": settings.model, "messages": payload},
            )
    except httpx2.HTTPError as error:
        raise SummaryError(f"Could not reach {url}: {error}") from error

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code >= 400:
        said = ""
        if isinstance(body.get("error"), dict):
            said = str(body["error"].get("message") or "")
        raise SummaryError(said or f"{url} answered {response.status_code}.")

    try:
        written = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise SummaryError("The reply did not contain a summary.") from None

    written = (written or "").strip()
    if not written:
        raise SummaryError("The model returned nothing.")
    return written


async def write(app, username: str) -> dict:
    """Build the numbers, ask, and keep both."""
    store: SettingsStore = app.state.settings
    settings = store.summary
    numbers = await facts(app)
    body = await ask(settings, messages(settings, numbers))

    row = await app.state.pool.fetchrow(
        """
        INSERT INTO summary (window_key, model, body, facts, written_by)
        VALUES ($1, $2, $3, $4::jsonb, $5)
        RETURNING id, created_at, window_key, model, body, written_by
        """,
        WINDOW.key,
        settings.model,
        body,
        json.dumps(numbers),
        username,
    )
    log.info("%s wrote a summary with %s", username, settings.model)
    return dict(row)


async def latest(pool: asyncpg.Pool) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT id, created_at, window_key, model, body, written_by
        FROM summary
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    return dict(row) if row else None


def age(created_at: datetime | None) -> str:
    """How long ago, in the same words the dashboard uses."""
    if created_at is None:
        return ""
    seconds = max(0, int((datetime.now(UTC) - created_at).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)} min ago"
    if seconds < 86400:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


__all__ = [
    "SummaryError",
    "age",
    "ask",
    "facts",
    "latest",
    "messages",
    "rainfall",
    "write",
]
