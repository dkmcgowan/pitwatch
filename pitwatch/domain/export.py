"""The history window as a spreadsheet.

Everything on the history page is a chart, and a chart answers the question it
was drawn for and nothing else. Somebody asking "what exactly happened between
five and six on Tuesday" has no way to get at it, and the person best placed to
answer a question about this pit is usually not sitting in front of this
application at all: a plumber wants the run lengths, a board wants a month of
call counts, and both of them want it in the program they already use.

So the same window the page is drawing, as rows, one sheet per kind of thing.
Nothing here is derived or rounded beyond what is stored, because the point of
an export is to hand over what we have rather than our opinion of it.

**Times are written in the building's own clock, without an offset.** Spreadsheets
have no real timezone type, and a column of UTC stamps against a building that
runs on Eastern time is a column somebody silently misreads by four hours. The
About sheet says which zone it is, once, where it cannot be lost.

**The running amps sheet holds only what a pump was doing while it ran.** The meter
reports whether or not anything is turning, and on this pit two readings in
three were the clamp watching a still motor. They are joined to the run they
belong to rather than filtered by a threshold, because a contactor closed on a
motor that is not turning reads zero during a run and that is precisely the
row worth keeping.

**It streams.** Even trimmed, that sheet is thousands of rows a day. Held
in memory that is tens of megabytes on a machine that also has a pump to
watch, so the workbook is written a row at a time to a temporary file and
handed to the browser from there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from pitwatch import clock

log = logging.getLogger(__name__)

# Excel refuses a sheet longer than 1,048,576 rows, and a spreadsheet that
# large is not one anybody opens anyway. Cut below that with room for the
# header, and say so on the sheet rather than truncating in silence: a export
# that quietly stops is worse than one that admits where it stopped.
ROW_LIMIT = 1_000_000


@dataclass(frozen=True, slots=True)
class Sheet:
    """One tab: what it is called, its column headings, and its rows."""

    title: str
    headings: tuple[str, ...]
    rows: list[tuple]
    # Which columns hold a moment, so they can be given a date format rather
    # than arriving as a number nobody recognizes.
    dates: tuple[int, ...] = ()
    truncated: bool = False


# Every ORDER BY below carries a tiebreaker, and they are not decoration.
#
# `ORDER BY ts` alone leaves rows that share an instant in whatever order the
# plan happens to produce, and this panel produces ties: the X-408 reports the
# pump run contact and the lead float in the same millisecond, five times in
# the first fortnight of real data. Two downloads of the same window then
# differ, which is exactly what somebody diffing two spreadsheets is trying to
# rule out. Found by exporting the same production window from two builds and
# comparing cell by cell.
CONTACTS = """
SELECT ts, channel, label, state
FROM io_event WHERE site_id = $4 AND ts >= $1 AND ts < $2
ORDER BY ts, channel LIMIT $3
"""

RUNS = """
SELECT started_at, ended_at, pump, duration_s, peak_current, steady_current, started_by
FROM pump_run WHERE site_id = $4 AND started_at >= $1 AND started_at < $2
ORDER BY started_at, pump LIMIT $3
"""

# Only the readings that belong to a run.
#
# The meter reports whether or not anything is turning, and on this pit a pump
# runs twelve seconds in every three minutes, so two readings in three are the
# clamp watching a motor sit still. Measured over a week: 64% of the rows were
# zero. A sheet that is mostly zero is a sheet somebody scrolls past.
#
# Joined to the run rather than filtered by a threshold, because the reading
# worth having is sometimes the low one: a contactor closed on a motor that is
# not turning reads zero *during a run*, and that is the whole point of the
# contactor rule. A threshold would throw away exactly the rows that matter.
# Two seconds either side catches the inrush that lands before the contact
# settles and the tail after it opens.
#
# A run's own start comes along so the readings can be grouped by run in a
# pivot, which is the question anybody opens this tab to ask.
AMPS = """
SELECT s.ts, s.channel, s.current, r.pump, r.started_at AS run_started
FROM em_sample s
JOIN pump_run r
  ON r.site_id = s.site_id
 AND r.pump = s.channel + 1
 AND s.ts >= r.started_at - interval '2 seconds'
 AND s.ts <= coalesce(r.ended_at, now()) + interval '2 seconds'
WHERE s.site_id = $4 AND s.ts >= $1 AND s.ts < $2
  AND r.started_at >= $1 - interval '1 hour' AND r.started_at < $2
ORDER BY s.ts, s.channel, r.started_at LIMIT $3
"""

# Alerts are not windowed the same way. One that opened a fortnight before the
# window and is still open is the most important row in the file, and cutting
# it because it started too early would be the one omission somebody acts on.
ALERTS = """
SELECT raised_at, cleared_at, rule, pump, severity, title, detail
FROM alert WHERE site_id = $3 AND (cleared_at IS NULL OR raised_at >= $1)
ORDER BY raised_at, rule, pump LIMIT $2
"""


def _local(when: datetime | None, zone: str) -> datetime | None:
    """A moment on the building's clock, with the offset dropped.

    Dropped rather than kept because spreadsheets have no timezone type and
    will either discard it or, worse, quietly shift the value.
    """
    if when is None:
        return None
    return clock.local(when, zone).replace(tzinfo=None)


async def gather(
    pool: asyncpg.Pool,
    site_id: int,
    since: datetime,
    until: datetime,
    zone: str,
    names: dict[int, str],
) -> list[Sheet]:
    """Every sheet in the workbook, in the order they should be read."""
    contacts = await pool.fetch(CONTACTS, since, until, ROW_LIMIT + 1, site_id)
    runs = await pool.fetch(RUNS, since, until, ROW_LIMIT + 1, site_id)
    amps = await pool.fetch(AMPS, since, until, ROW_LIMIT + 1, site_id)
    alerts = await pool.fetch(ALERTS, since, ROW_LIMIT + 1, site_id)

    def cut(rows):
        return rows[:ROW_LIMIT], len(rows) > ROW_LIMIT

    contacts, contacts_cut = cut(contacts)
    runs, runs_cut = cut(runs)
    amps, amps_cut = cut(amps)
    alerts, alerts_cut = cut(alerts)

    return [
        Sheet(
            title="Contacts",
            headings=("Time", "Input", "What it is", "State"),
            # "Closed" and "Open" rather than TRUE and FALSE. The column is read
            # by somebody holding a wiring diagram, and that is the word on it.
            rows=[
                (
                    _local(r["ts"], zone),
                    r["channel"],
                    r["label"],
                    "Closed" if r["state"] else "Open",
                )
                for r in contacts
            ],
            dates=(0,),
            truncated=contacts_cut,
        ),
        Sheet(
            title="Pump runs",
            headings=(
                "Started",
                "Ended",
                "Pump",
                "Seconds",
                "Peak amps",
                "Steady amps",
                "Detected by",
            ),
            rows=[
                (
                    _local(r["started_at"], zone),
                    _local(r["ended_at"], zone),
                    names.get(r["pump"], f"Pump {r['pump']}"),
                    r["duration_s"],
                    r["peak_current"],
                    r["steady_current"],
                    r["started_by"],
                )
                for r in runs
            ],
            dates=(0, 1),
            truncated=runs_cut,
        ),
        Sheet(
            # "Running amps" rather than "Amps", because that is what the tab
            # holds and a name that oversells its contents is the reason
            # somebody scrolls looking for readings that were never there.
            title="Running amps",
            headings=("Time", "Pump", "Channel", "Amps", "Run started"),
            rows=[
                (
                    _local(r["ts"], zone),
                    names.get(r["pump"], f"Pump {r['pump']}"),
                    r["channel"],
                    r["current"],
                    _local(r["run_started"], zone),
                )
                for r in amps
            ],
            dates=(0, 4),
            truncated=amps_cut,
        ),
        Sheet(
            title="Alerts",
            headings=("Raised", "Cleared", "Rule", "Pump", "Severity", "What", "Message"),
            rows=[
                (
                    _local(r["raised_at"], zone),
                    _local(r["cleared_at"], zone),
                    r["rule"],
                    names.get(r["pump"], "") if r["pump"] else "",
                    r["severity"],
                    r["title"],
                    r["detail"],
                )
                for r in alerts
            ],
            dates=(0, 1),
            truncated=alerts_cut,
        ),
    ]


def write(path: str, sheets: list[Sheet], about: list[tuple[str, str]]) -> None:
    """The workbook, a row at a time.

    `constant_memory` writes each row out as it arrives and keeps none of them,
    which is what makes a quarter of a million meter readings survivable on a
    small machine. The cost is that rows have to be written in order and cannot
    be revisited, which nothing here wants to do anyway.
    """
    import xlsxwriter

    book = xlsxwriter.Workbook(path, {"constant_memory": True, "default_date_format": None})
    try:
        head = book.add_format({"bold": True, "bottom": 1})
        stamp = book.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})

        # The About sheet first, so the file opens on what it is rather than on
        # a wall of numbers with no window, zone or date attached to them.
        sheet = book.add_worksheet("About")
        sheet.set_column(0, 0, 22)
        sheet.set_column(1, 1, 60)
        for row, (name, value) in enumerate(about):
            sheet.write(row, 0, name, head if row == 0 else None)
            sheet.write(row, 1, value)

        for one in sheets:
            sheet = book.add_worksheet(one.title)
            for column, heading in enumerate(one.headings):
                sheet.write(0, column, heading, head)
            sheet.set_column(0, 1, 20)
            # Frozen under the headings, because scrolling a hundred thousand
            # rows away from them makes the columns unreadable.
            sheet.freeze_panes(1, 0)
            for index, values in enumerate(one.rows, start=1):
                for column, value in enumerate(values):
                    if value is None:
                        continue
                    if column in one.dates:
                        sheet.write_datetime(index, column, value, stamp)
                    else:
                        sheet.write(index, column, value)
            if one.truncated:
                sheet.write(
                    len(one.rows) + 2,
                    0,
                    f"Stopped at {ROW_LIMIT:,} rows, which is as many as a sheet holds. "
                    f"Ask for a shorter window to see the rest.",
                )
    finally:
        book.close()


def filename(window: str, when: datetime | None = None) -> str:
    """What the browser saves it as. Sorts by date in a folder."""
    day = (when or datetime.now(UTC)).strftime("%Y-%m-%d")
    return f"pitwatch-{window}-{day}.xlsx"


__all__ = ["ROW_LIMIT", "Sheet", "filename", "gather", "write"]
