"""The written summary: what the numbers are, and what a model made of them.

Two halves that stay apart on purpose. Everything up to `facts` is arithmetic
this application can defend, and it is stored beside whatever comes back, so a
summary read a month later can be checked against what it was actually looking
at. Only the last step leaves the building.

What is sent: the description somebody wrote on the settings page, and a page
of numbers. Not the site name, not the address, not a single account name.
Nobody needs a street address to say whether a pump is drawing more than it
did last week, and the one thing the owner of this pit has been clear about is
that his address is not public.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import asyncpg
import httpx2

from pitwatch import domain
from pitwatch.domain import series
from pitwatch.domain.history import CurrentHistory
from pitwatch.schemas import SummarySettings
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

# Long enough for a slow model on a busy afternoon, short enough that a browser
# waiting on it has not given up first.
TIMEOUT_S = 90.0

WINDOW = series.WINDOWS["7d"]

DAILY = """
SELECT time_bucket('1 day', ts)                        AS day,
       max(current)                                    AS peak,
       avg(current) FILTER (WHERE current >= $3)       AS running_mean,
       count(*)     FILTER (WHERE current >= $3)       AS running_samples
FROM em_sample
WHERE channel = $1 AND ts > now() - $2::interval
GROUP BY 1
ORDER BY 1
"""

INSTRUCTIONS = (
    "You are reading a week of monitoring data from a duplex ejector pump "
    "panel in a building, for the person responsible for keeping it running. "
    "Say whether the system looks healthy, what changed over the week, and "
    "anything worth watching or acting on. Be specific and use the numbers. "
    "Where the data is too thin to support a conclusion, say so plainly "
    "rather than hedging. Never invent a reading that is not in the data. "
    "Four short paragraphs at most, plain text, no headings and no bullet "
    "points."
)


async def facts(app, window: series.Window = WINDOW) -> dict:
    """The week in numbers, in the shape the model is given it.

    Deliberately small. A week of raw readings is tens of thousands of rows and
    says nothing a daily figure does not; the point of this is to be checkable
    by somebody reading it later, not to be exhaustive.
    """
    store: SettingsStore = app.state.settings
    pool: asyncpg.Pool = app.state.pool
    clamp = store.shelly.clamp_for_pump
    history: CurrentHistory | None = getattr(app.state, "history", None)

    pumps = []
    for number, pump in store.pumps.by_number.items():
        channel = clamp[number]
        starts = await series.starts_series(pool, channel, window, domain.RUNNING_AMPS)
        try:
            rows = await pool.fetch(DAILY, channel, window.span, domain.RUNNING_AMPS)
        except (asyncpg.PostgresError, OSError) as error:
            log.warning("Could not read the daily figures: %s", error)
            rows = []

        by_day = {at.date().isoformat(): count for at, count in starts}
        days = []
        for row in rows:
            day = row["day"].date().isoformat()
            days.append(
                {
                    "day": day,
                    "starts": by_day.pop(day, 0),
                    "peak_amps": round(float(row["peak"] or 0.0), 2),
                    "running_amps": (
                        round(float(row["running_mean"]), 2) if row["running_mean"] else None
                    ),
                    "readings_while_running": int(row["running_samples"] or 0),
                }
            )
        # A day with starts but no readings row cannot happen, but a day with
        # neither is simply absent, and absent is the honest answer: this is a
        # meter that reports when something changes.
        for day, count in by_day.items():
            days.append({"day": day, "starts": count})

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
                "starts_this_week": sum(count for _, count in starts),
                "typical_load": typical,
                "days": sorted(days, key=lambda entry: entry["day"]),
            }
        )

    inputs = store.inputs
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

    devices = []
    try:
        rows = await pool.fetch("SELECT device, online, last_seen, last_error FROM device_status")
    except (asyncpg.PostgresError, OSError):
        rows = []
    for row in rows:
        devices.append(
            {
                "device": row["device"],
                "online": row["online"],
                "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
                "last_error": row["last_error"],
            }
        )

    return {
        "window": window.title,
        "generated_at": datetime.now(UTC).isoformat(),
        "running_threshold_amps": domain.RUNNING_AMPS,
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
    "write",
]
