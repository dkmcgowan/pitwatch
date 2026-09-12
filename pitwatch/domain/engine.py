"""Deciding that something is wrong, and telling the people who said they
wanted to know.

Everything up to here has been about knowing. This is the part that was the
point: a pit that floods at three in the morning while a correct dashboard
nobody is looking at draws a red lamp has not been monitored, it has been
recorded.

**One open alert per rule, enforced by the database.** `alert_one_open_per_rule`
is a unique index over (rule, pump) where `cleared_at IS NULL`, so raising the
same alert twice cannot happen and does not have to be prevented here. The
insert is `ON CONFLICT DO NOTHING RETURNING id`: a row comes back only when the
alert is genuinely new, and that returned row is the only thing that ever
causes a message to be sent. A rule that is true for six hours sends once.

**Clearing is the same shape.** The update returns a row only if something was
actually open, so a clear cannot be announced for an alert nobody was told
about.

**Silence on missing data, never a guess.** Every rule that cannot be evaluated
-- no contact assigned, no clamp fitted, not enough history -- returns None and
nothing happens. The alternative is a rule that fires because a wire is
missing, which teaches somebody to ignore it, and an ignored alert is worse
than no alert because it costs the same and buys nothing.

**A failed send is recorded, not raised.** The notification row carries its own
status. A broken SMTP server must not stop the next rule being evaluated, and
must not stop the alert itself being written down.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from pitwatch import clock, domain
from pitwatch.domain import alerts as specs
from pitwatch.notify import dispatch

log = logging.getLogger(__name__)

# How often the rules are swept when nothing has changed.
#
# The contact rules do not wait for this: a closing float runs the sweep
# immediately, because the whole argument for reading contacts rather than
# current was that the panel says so at the moment it happens. The tick is for
# the rules made of elapsed time -- nothing has run, ran too long, a pump that
# has stopped taking its turn -- which become true while nothing is happening
# and would otherwise be noticed at the next float rather than on the clock.
SWEEP_S = 30.0

# How long a run has to be open before its current is worth judging.
#
# A motor draws its inrush and comes up to speed, and the meter reports when it
# feels like it, so the first seconds of a run say nothing about whether the
# thing is turning. This only has to outlast that.
SETTLE_S = 6.0


def _spell(seconds: float) -> str:
    """A number of seconds as somebody would say it out loud.

    Messages ask for a duration and the engine had only ever supplied a bare
    count of seconds under a different name, so the placeholder went out with
    its braces still on.
    """
    total = int(seconds)
    if total < 60:
        return f"{total} s"
    minutes, rest = divmod(total, 60)
    return f"{minutes} min {rest} s" if rest else f"{minutes} min"


@dataclass(frozen=True, slots=True)
class Finding:
    """A rule that has something to say, and what to say about it."""

    pump: int | None = None
    values: dict | None = None


class AlertEngine:
    """Sweeps the rules, writes what it finds, and tells whoever asked."""

    def __init__(self, pool: asyncpg.Pool, store, live, live_io, history, recent_runs) -> None:
        self._pool = pool
        self._store = store
        self._live = live
        self._live_io = live_io
        # The same cached readers the dashboard uses, handed in rather than
        # reached for. Two rules are about a trend rather than a moment, and
        # recomputing a month of medians every thirty seconds to answer them
        # would be a lot of database for a number that moves once a week.
        self._history = history
        self._recent = recent_runs
        # When the panel's own alarm was first seen up, so it can be held back
        # briefly to see whether something that carries detail explains it,
        # and when it was last seen to drop, so the gaps in a pulsing alarm
        # are not each read as the alarm ending.
        self._panel_alert_since: datetime | None = None
        self._panel_alert_quiet_since: datetime | None = None
        # Whether this alarm was explained by something else when it first
        # came up, decided once and kept for as long as the alarm lasts.
        self._panel_alert_explained: bool | None = None
        self._wake = asyncio.Event()
        # When a rule that deferred wants looking at again.
        #
        # A rule that holds back is saying "ask me later", and until this
        # existed nothing ever did. The panel alarm was tripped by hand on
        # 2026-09-08 and held for fourteen seconds: the contact change nudged a
        # sweep, the sweep started the five second hold and returned nothing,
        # and the next look was the thirty second tick, which arrived after the
        # alarm had already cleared. Any panel alarm shorter than a tick was
        # invisible, on the contact that carries the controller's own alarm.
        self._recheck_at: float | None = None

    # -- when it runs -------------------------------------------------------

    def nudge(self) -> None:
        """Something changed on the panel, so sweep now rather than on the
        tick. Called from the ingest path, which must not be made to wait."""
        self._wake.set()

    def _ask_again_in(self, seconds: float) -> None:
        """A rule saying it deferred and wants another look.

        The soonest request wins, because two rules holding for different
        lengths both have to be answered on time.
        """
        try:
            at = asyncio.get_running_loop().time() + max(0.0, seconds)
        except RuntimeError:  # pragma: no cover -- swept outside a loop
            return
        if self._recheck_at is None or at < self._recheck_at:
            self._recheck_at = at

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # Three things can end the wait: the panel changed, the tick
            # expired, or we are shutting down. Waiting on the nudge alone was
            # the first version, and it meant a stopping supervisor sat here
            # for the length of a tick before noticing, because nothing sets
            # the nudge on the way out. Every other reader in this package
            # waits on its stop event for exactly that reason.
            #
            # The first pass comes after the wait, not before it. Booting is
            # the one moment nothing can usefully be judged: ingest has not
            # connected and the contacts have not been primed, so every rule
            # would be reading a state that is empty rather than calm.
            # A rule that deferred gets its second look when it asked for it,
            # rather than waiting out a whole tick.
            timeout = SWEEP_S
            if self._recheck_at is not None:
                waiting = self._recheck_at - asyncio.get_running_loop().time()
                timeout = max(0.05, min(timeout, waiting))
            await _first_of(self._wake.wait(), stop.wait(), timeout=timeout)
            if stop.is_set():
                return
            self._wake.clear()
            # Cleared before the sweep, so a rule that still wants another look
            # says so again rather than inheriting one it has finished with.
            self._recheck_at = None
            try:
                await self.sweep()
            except (asyncpg.PostgresError, OSError) as error:
                log.warning("Could not sweep the alert rules: %s", error)
            except Exception:
                # A mistake in one rule must not stop the other fourteen being
                # evaluated, and must not take the sweep down for good. Logged
                # with a traceback rather than swallowed, which is how a broken
                # rule stays findable.
                log.exception("A rule raised while being evaluated")

    # -- the sweep ----------------------------------------------------------

    async def sweep(self) -> None:
        rules = self._store.alerts
        for key in specs.BY_KEY:
            rule = getattr(rules, key, None)
            if rule is None or not rule.enabled:
                continue
            check = getattr(self, f"_check_{key}", None)
            if check is None:
                continue
            findings = await check(rule)
            await self._settle(key, rule, findings)

    async def _settle(self, key: str, rule, findings) -> None:
        """Raise what is true, clear what is not, for every pump the rule
        covers. A rule that returned None is one that could not be evaluated,
        and neither raises nor clears: it has no opinion, which is different
        from having the opinion that all is well."""
        if findings is None:
            return
        for pump, finding in findings.items():
            if finding is None:
                await self._clear(key, rule, pump)
            else:
                await self._raise(key, rule, pump, finding)

    # -- writing it down ----------------------------------------------------

    async def _raise(self, key: str, rule, pump: int | None, finding: Finding) -> None:
        spec = specs.BY_KEY[key]
        values = dict(finding.values or {})
        values.setdefault("site", self._store.site.where or "the pit")
        values.setdefault("time", clock.at(datetime.now(UTC), self._store.site.timezone))
        if pump:
            values.setdefault("pump", self._store.pumps.by_number[pump].name)
        detail = specs.fill(rule.message, values)

        alert_id = await self._pool.fetchval(
            """
            INSERT INTO alert (rule, severity, pump, title, detail, context)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            key,
            rule.severity.value,
            pump,
            spec.title,
            detail,
            _as_json(values),
        )
        # Nothing came back, so this alert was already open. The condition is
        # still true and that is not news.
        if alert_id is None:
            return

        log.warning("ALERT %s: %s", key, detail)
        await self._notify(alert_id, "raised", rule, detail)

    async def _clear(self, key: str, rule, pump: int | None) -> None:
        row = await self._pool.fetchrow(
            """
            UPDATE alert SET cleared_at = now()
            WHERE rule = $1 AND pump IS NOT DISTINCT FROM $2 AND cleared_at IS NULL
            RETURNING id, detail
            """,
            key,
            pump,
        )
        if row is None:
            return

        log.info("Cleared %s", key)
        if not rule.tell_when_it_clears:
            return
        site = self._store.site.where or "the pit"
        spec = specs.BY_KEY[key]
        if spec.cleared_message:
            # Some rules going away is not the same as them being over, and
            # for those the generic sentence opens with a word that is not
            # true. An overload resetting is the case: the relay is fine and
            # the pump is still out of the rotation.
            values: dict[str, object] = {
                "site": site,
                "time": clock.at(datetime.now(UTC), self._store.site.timezone),
            }
            if pump:
                values["pump"] = self._store.pumps.by_number[pump].name
                values["cover"] = self._cover(pump)
            # A value that came back empty leaves a gap where it was, and a
            # message with two spaces in the middle looks like a bug to the
            # person reading it, which for a message about a pump being out of
            # service is exactly the wrong impression.
            said = " ".join(specs.fill(spec.cleared_message, values).split())
        else:
            said = f"Cleared at {site}: {spec.title}."
        await self._notify(row["id"], "cleared", rule, said)

    # -- telling somebody ---------------------------------------------------

    async def _notify(self, alert_id: int, event: str, rule, message: str) -> None:
        """Tell whoever asked to be told. The doing of it is in notify.dispatch,
        which the daily health check uses as well."""
        await dispatch.tell(
            self._pool,
            self._store,
            message=message,
            severity=rule.severity,
            event=event,
            admins_only=rule.admins_only,
            alert_id=alert_id,
        )

    # -- what the contacts say ----------------------------------------------

    def _contact(self, role: str) -> bool | None:
        channel = self._store.mqtt.channel_for(role)
        if not channel:
            return None
        return self._live_io.state_of(channel)

    # -- filling in a message -----------------------------------------------
    #
    # Every placeholder a rule offers has to be supplied by somebody, and for
    # four of them nobody was. `fill` leaves what it does not know alone, on
    # the argument that a slightly odd alert beats a silent one, so the
    # failure was quiet: high water sent "The top float is wet,
    # {pumps_state}." to a phone, and nothing in any test noticed because
    # nothing asserted on the finished sentence.

    def _pumps_state(self) -> str:
        """Which pumps are turning, for the high water message."""
        one, two = self._contact("pump1_run"), self._contact("pump2_run")
        if one is None and two is None:
            return "the run contacts are not wired"
        names = self._store.pumps.by_number
        running = [names[number].name for number, on in ((1, one), (2, two)) if on]
        if len(running) == 2:
            return "both pumps are running"
        if running:
            return f"only {running[0]} is running"
        return "neither pump is running"

    def _cover(self, pump: int) -> str:
        """What is left while this pump is out, for the message that says it
        is not back yet. The reassuring half and the frightening half are the
        same sentence with a different other pump behind it."""
        other = 2 if pump == 1 else 1
        name = self._store.pumps.by_number[other].name
        tripped = self._contact(f"pump{other}_fault")
        if tripped is None:
            return ""
        if tripped:
            return f"{name} is out as well, so nothing is pumping at all."
        return f"{name} is covering on its own until then."

    def _overload_label(self, pump: int) -> str:
        """What the overload relay is called on the panel, for somebody
        standing in front of it looking for the right one to reset."""
        channel = self._store.mqtt.channel_for(f"pump{pump}_fault")
        label = self._store.mqtt.label_for(channel) if channel else None
        return label or "the overload relay"

    async def _check_high_water(self, rule) -> dict | None:
        wet = self._contact("high_water")
        if wet is None:
            return None
        return {None: Finding(values={"pumps_state": self._pumps_state()}) if wet else None}

    async def _check_overload(self, rule) -> dict | None:
        found = {}
        for pump in (1, 2):
            tripped = self._contact(f"pump{pump}_fault")
            if tripped is None:
                continue
            found[pump] = (
                Finding(pump=pump, values={"overload": self._overload_label(pump)})
                if tripped
                else None
            )
        return found or None

    async def _check_both_overloads(self, rule) -> dict | None:
        """Nothing left to pump with.

        Not two overloads. The per pump alerts already say which relay to go
        and reset; this one says there is no spare left and the pit fills from
        here. It is the only state on this panel where doing nothing has a
        deadline.
        """
        one = self._contact("pump1_fault")
        two = self._contact("pump2_fault")
        if one is None or two is None:
            return None
        return {None: Finding() if (one and two) else None}

    async def _check_both_pumps(self, rule) -> dict | None:
        one, two = self._contact("pump1_run"), self._contact("pump2_run")
        if one is None or two is None:
            return None
        return {None: Finding() if (one and two) else None}

    async def _check_panel_alert(self, rule) -> dict | None:
        """The controller's own alarm, held briefly, through the gaps.

        It carries no detail -- a power cut, an open door and half a dozen
        faults all raise the same contact -- so it waits to see whether
        something that does carry detail explains it. Two alerts for one event
        means the vaguer one is read first.

        The hold counts from the first time the alarm was seen up and keeps
        counting through a gap shorter than the rule's pulse gap, because the
        panel pulses this output rather than holding it. A single blip and then
        nothing is still filtered: the gap runs out before the hold does, and
        the hold is thrown away without ever having been reached.

        **Explained once is explained for good.** Whether anything else
        accounted for this alarm is decided when the hold completes and then
        kept until the alarm itself ends. The alternative is re-asking every
        sweep, which means the moment the overload is reset -- while the panel
        alarm is still latched, because it is -- nothing explains it any more
        and a second alert goes out saying so. That is the wrong message at the
        wrong time: the overload did explain it, and the person reading it has
        just finished dealing with the overload. On 2026-09-12 that window was
        6.6 s and 8.4 s wide on two real trips, and it was missed both times
        only because no sweep happened to land in it.

        Something new raising its own alert is still heard, because that alert
        speaks for itself. This only decides whether the vague one is worth
        adding to it.
        """
        raised = self._contact("system_alert")
        if raised is None:
            return None

        now = datetime.now(UTC)

        if raised:
            self._panel_alert_quiet_since = None
            if self._panel_alert_since is None:
                self._panel_alert_since = now
        elif self._panel_alert_since is None:
            return {None: None}
        else:
            if self._panel_alert_quiet_since is None:
                self._panel_alert_quiet_since = now
            quiet = (now - self._panel_alert_quiet_since).total_seconds()
            if quiet < rule.pulse_gap_s:
                # Might be the dark half of a pulse rather than the end of the
                # alarm. Say nothing either way and come back when the gap has
                # gone on long enough to mean something.
                self._ask_again_in(rule.pulse_gap_s - quiet)
                return None
            self._panel_alert_since = None
            self._panel_alert_quiet_since = None
            self._panel_alert_explained = None
            return {None: None}

        held = (now - self._panel_alert_since).total_seconds()
        if held < rule.hold_s:
            # Ask to be looked at again when the hold is up. Without this the
            # hold is not a delay, it is a filter that drops every alarm
            # shorter than a sweep.
            self._ask_again_in(rule.hold_s - held)
            return None

        if self._panel_alert_explained is None:
            self._panel_alert_explained = bool(
                await self._pool.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM alert
                        WHERE cleared_at IS NULL AND rule <> 'panel_alert'
                          AND severity IN ('warning', 'critical')
                    )
                    """
                )
            )
        return {None: None if self._panel_alert_explained else Finding()}

    # Float activity and a pump starting are not swept. They are moments, not
    # conditions: there is nothing to be true later and nothing to clear, so a
    # sweep would either miss them between ticks or report them forever. They
    # are handled by on_events, off the ingest path, at the instant they
    # happen. Both are off by default and both exist for an afternoon of
    # commissioning rather than a week of running.

    async def on_events(self, events) -> None:
        """The two rules that are about something happening rather than
        something being wrong.

        Written down as an alert that is already closed. A row that never
        clears would hold the one-open-per-rule index forever and the second
        float of the day would be silent; a row with no record at all would
        leave the history page unable to say a message was ever sent.
        """
        rules = self._store.alerts
        inputs = self._store.mqtt
        for event in events:
            if not event.state:
                continue
            role = next(
                (mapped.role for mapped in inputs.channels if mapped.channel == event.channel),
                "",
            )
            if role in ("lead_float", "lag_float", "high_water"):
                rule, values = rules.float_activity, {"float": event.label}
            elif role in ("pump1_run", "pump2_run"):
                pump = 1 if role == "pump1_run" else 2
                rule = rules.pump_running
                values = {"pump": self._store.pumps.by_number[pump].name}
            else:
                continue
            if not rule.enabled:
                continue
            with contextlib.suppress(asyncpg.PostgresError, OSError):
                await self._announce(role, rule, values)

    async def _announce(self, role: str, rule, values: dict) -> None:
        """An alert that is raised and cleared in the same breath."""
        values = dict(values)
        values.setdefault("site", self._store.site.where or "the pit")
        values.setdefault("time", clock.at(datetime.now(UTC), self._store.site.timezone))
        detail = specs.fill(rule.message, values)
        key = "float_activity" if "float" in values else "pump_running"

        alert_id = await self._pool.fetchval(
            """
            INSERT INTO alert (rule, severity, title, detail, context, cleared_at)
            VALUES ($1, $2, $3, $4, $5::jsonb, now())
            RETURNING id
            """,
            key,
            rule.severity.value,
            specs.BY_KEY[key].title,
            detail,
            _as_json(values),
        )
        await self._notify(alert_id, "raised", rule, detail)

    # -- what the clock says ------------------------------------------------

    async def _check_run_too_long(self, rule) -> dict | None:
        if not rule.longer_than_ms:
            return None
        rows = await self._pool.fetch(
            """
            SELECT pump, extract(epoch FROM now() - started_at) AS running_s
            FROM pump_run WHERE ended_at IS NULL
            """
        )
        running = {row["pump"]: row["running_s"] for row in rows}
        found = {}
        for pump in (1, 2):
            seconds = running.get(pump)
            over = seconds is not None and seconds * 1000 >= rule.longer_than_ms
            found[pump] = (
                Finding(
                    pump=pump,
                    values={
                        "seconds": int(seconds or 0),
                        "duration": _spell(seconds or 0),
                    },
                )
                if over
                else None
            )
        return found

    async def _check_nothing_has_run(self, rule) -> dict | None:
        if not rule.quiet_minutes:
            return None
        last = await self._pool.fetchval("SELECT max(started_at) FROM pump_run")
        if last is None:
            # Nothing has ever run, which on a fresh install is not news.
            return None
        quiet_min = (datetime.now(UTC) - last).total_seconds() / 60
        over = quiet_min >= rule.quiet_minutes
        return {None: Finding(values={"quiet": f"{quiet_min / 60:.1f} h"}) if over else None}

    async def _check_pump_idle(self, rule) -> dict | None:
        """One pump sitting out while the other works.

        Only meaningful once the other pump has actually been running, which is
        what makes this different from nothing having run: a quiet pit is not
        an idle pump, and this must not fire on a dry week.

        **A pump with no runs on record has not been idle, it has not been
        watched.** This used to invent a number for that case, one hour past
        the threshold, which meant "nothing recorded" was read as "definitely
        broken". The first run after the readings were wiped on 2026-09-08 sent
        a text saying pump 2 had not run in 25 hours, twenty two minutes after
        the table was emptied. A fresh installation would have done the same on
        its first ever call for water.

        So an unseen pump is measured from when there was anything to see. The
        claim is "has not run in N hours", and making it honestly requires N
        hours of watching. It still fires eventually on a pump that genuinely
        never starts, once the record is old enough to support the sentence.
        """
        if not rule.idle_hours:
            return None
        rows = await self._pool.fetch(
            "SELECT pump, max(started_at) AS last FROM pump_run GROUP BY pump"
        )
        last = {row["pump"]: row["last"] for row in rows}
        # The oldest run on record, which is how far back the evidence goes.
        watching_since = await self._pool.fetchval("SELECT min(started_at) FROM pump_run")
        now = datetime.now(UTC)
        found = {}
        for pump in (1, 2):
            other = last.get(2 if pump == 1 else 1)
            mine = last.get(pump)
            if other is None or (now - other).total_seconds() / 3600 >= rule.idle_hours:
                # The other one has not been working either, so nothing here
                # says this pump is the problem.
                found[pump] = None
                continue
            if mine is not None:
                idle_h = (now - mine).total_seconds() / 3600
            elif watching_since is not None:
                # Never seen. That is only worth reporting once the record
                # reaches back further than the threshold, because before then
                # the sentence would be describing our own short memory.
                idle_h = (now - watching_since).total_seconds() / 3600
            else:
                found[pump] = None
                continue
            over = idle_h >= rule.idle_hours
            found[pump] = Finding(pump=pump, values={"hours": int(idle_h)}) if over else None
        return found

    async def _check_short_cycling(self, rule) -> dict | None:
        if not rule.restart_within_ms:
            return None
        found = {}
        for pump in (1, 2):
            gaps = await self._pool.fetch(
                """
                SELECT extract(epoch FROM started_at - lag(ended_at)
                       OVER (ORDER BY started_at)) AS gap_s
                FROM pump_run
                WHERE pump = $1 AND started_at > now() - interval '2 hours'
                ORDER BY started_at DESC LIMIT $2
                """,
                pump,
                rule.times_in_a_row,
            )
            measured = [row["gap_s"] for row in gaps if row["gap_s"] is not None]
            tight = len(measured) >= rule.times_in_a_row and all(
                gap * 1000 <= rule.restart_within_ms for gap in measured
            )
            found[pump] = (
                Finding(
                    pump=pump,
                    values={
                        "gap": _spell(max(measured)),
                        "times": rule.times_in_a_row,
                    },
                )
                if tight
                else None
            )
        return found

    # -- what the clamps say ------------------------------------------------

    async def _check_contactor_no_current(self, rule) -> dict | None:
        """A closed contactor drawing nothing: a motor that is not turning.

        The reason for having both a contact and a clamp, and the one rule that
        needs them to disagree. It stays quiet unless the clamp has proved it
        can see current at some point, because a channel that has never read
        above the running threshold is far more likely to be a CT nobody fitted
        than a pump that has never once worked, and firing on an unfitted CT
        every time the pump runs is how somebody learns to ignore this.
        """
        found = {}
        for pump in (1, 2):
            running = self._contact(f"pump{pump}_run")
            channel = self._store.mqtt.clamp_for_pump.get(pump)
            if running is None or channel is None:
                continue
            proven = await self._pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM em_sample WHERE channel = $1 AND current >= $2)",
                channel,
                domain.RUNNING_AMPS,
            )
            if not proven:
                continue

            open_since = await self._pool.fetchval(
                "SELECT extract(epoch FROM now() - started_at) FROM pump_run"
                " WHERE pump = $1 AND ended_at IS NULL",
                pump,
            )
            sample = self._live.samples.get(channel)
            amps = sample.current if sample else None
            dead = (
                running
                and open_since is not None
                and open_since >= SETTLE_S
                and amps is not None
                and amps < domain.RUNNING_AMPS
            )
            found[pump] = Finding(pump=pump, values={"amps": f"{amps:.1f}"}) if dead else None
        return found or None

    async def _check_over_current(self, rule) -> dict | None:
        found = {}
        for pump in (1, 2):
            limit = getattr(rule, f"pump{pump}_amps", None)
            channel = self._store.mqtt.clamp_for_pump.get(pump)
            if not limit or channel is None:
                continue
            rows = await self._pool.fetch(
                "SELECT current FROM em_sample WHERE channel = $1 AND current IS NOT NULL"
                " ORDER BY ts DESC LIMIT $2",
                channel,
                rule.readings,
            )
            readings = [row["current"] for row in rows]
            over = len(readings) >= rule.readings and all(value >= limit for value in readings)
            found[pump] = (
                Finding(pump=pump, values={"amps": f"{readings[0]:.1f}", "threshold": limit})
                if over
                else None
            )
        return found or None

    async def _check_load_drift(self, rule) -> dict | None:
        if not rule.climb_amps:
            return None
        if self._history is None:
            return None
        found = {}
        for pump in (1, 2):
            channel = self._store.mqtt.clamp_for_pump.get(pump)
            if channel is None:
                continue
            typical = await self._history.typical(self._pool, channel, domain.RUNNING_AMPS)
            climbed = typical.drift is not None and typical.drift >= rule.climb_amps
            found[pump] = (
                Finding(
                    pump=pump,
                    values={
                        "amps": f"{typical.median:.1f}",
                        "was": f"{typical.earlier_median:.1f}",
                    },
                )
                if climbed
                else None
            )
        return found or None

    async def _check_run_drift(self, rule) -> dict | None:
        if not rule.longer_by_s:
            return None
        if self._recent is None:
            return None
        found = {}
        for pump in (1, 2):
            if not self._store.mqtt.channel_for(f"pump{pump}_run"):
                continue
            recent = await self._recent.from_contacts(self._pool, pump, self._store.site.timezone)
            moved = recent.duration_drift_s
            longer = moved is not None and moved >= rule.longer_by_s
            found[pump] = (
                Finding(
                    pump=pump,
                    values={
                        "seconds": f"{recent.typical_duration_s:.0f}",
                        "was": f"{recent.typical_duration_s - moved:.0f}",
                    },
                )
                if longer
                else None
            )
        return found or None

    async def _check_device_offline(self, rule) -> dict | None:
        """One alert, naming whatever is not answering.

        Not one per device, because the only discriminator the alert table has
        is a pump column and a device is not a pump. Riding on it would put a
        meter's name in a field every other rule and the history page read as a
        motor. The alert is about PitWatch not watching the pumps, which is
        equally true whichever box went quiet, and the detail says which.
        """
        watched = self._watched_devices()
        if not watched:
            return None
        rows = await self._pool.fetch("SELECT device, online FROM device_status")
        gone = [
            watched[row["device"]] for row in rows if row["device"] in watched and not row["online"]
        ]
        return {None: Finding(values={"device": " and ".join(gone)}) if gone else None}

    def _watched_devices(self) -> dict[str, str]:
        """The rows in device_status this rule is about, and what to call them.

        Built from the settings rather than from a hardcoded list of device
        names. It was a hardcoded list once, and when the device names changed
        the lookup matched nothing, found nothing offline, and reported all
        clear: an alert that can only ever find nothing reads as working right
        up until the day it is needed.

        The health checks and nothing else. A clamp has a device_status row of
        its own, and it only ever changes state when the broker connection
        does, because that is the one event that reports every source at once.
        The health checks change state for that *and* for a device that has
        gone quiet on a live connection, so watching them covers both and
        watching the clamps as well says the same thing twice.

        Weather is deliberately not in here. It is not a device on the panel
        and its own card says when it went stale.
        """
        mqtt = self._store.mqtt
        if not (mqtt.enabled and mqtt.host):
            return {}
        return {
            f"health{index}": check.name or f"device {index + 1}"
            for index, check in enumerate(mqtt.health)
            if check.configured
        }


async def _first_of(*waits, timeout: float) -> None:
    """Wait until the first of several things happens, or the timeout."""
    tasks = [asyncio.ensure_future(wait) for wait in waits]
    try:
        await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _as_json(values: dict) -> str:
    import json

    return json.dumps({key: str(value) for key, value in values.items()})
