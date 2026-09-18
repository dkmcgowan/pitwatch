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
import itertools
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from pitwatch import clock, domain
from pitwatch.domain import alerts as specs
from pitwatch.notify import dispatch
from pitwatch.schemas import Severity

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

# What a still-sounding alarm looks like, and how often to answer one.
#
# This controller flashes its alarm output while nobody has acknowledged it and
# holds it steady once somebody has, so the flashing is not a detail of the
# signal, it is the signal: it means the panel is still asking. Answering that
# directly is simpler than any proxy for it, and it is what somebody standing
# in the doorway reads off the lamp.
#
# Three edges inside three seconds. The wave is half a second each way, so a
# real one clears that in about a second and a half, and a single blip cannot:
# two edges is a flicker and this wants a rhythm.
#
# Then a pause before answering again. The panel takes a moment to go steady
# after the button, and without a pause the sweep that runs in between would
# press a second time at an alarm already dealt with. Long enough to cover
# that, short enough that a press which did not land is retried while the horn
# is still going.
PULSING_WITHIN_S = 3.0
PULSING_EDGES = 3
HUSH_AGAIN_AFTER_S = 5.0

# Telling this panel's two pulses apart.
#
# It puts more than one meaning on its single alarm contact and distinguishes
# them by rate. An unacknowledged alarm is symmetric at about one hertz, half a
# second each way. A yearly service reminder is 2.00 s closed and 3.00 s open,
# measured on 2026-09-16 over 6,314 cycles without varying by more than a
# hundredth. The manufacturer confirmed the slow one is a maintenance prompt,
# deliberately kept too short to trip anything that would stop the pumps.
#
# The two are far enough apart that a threshold between them needs no care:
# gaps of half a second against gaps of two to three. Fourteen seconds of
# window catches five or six edges of the slow pattern, which is enough to
# take a median and not be fooled by one stray transition.
CADENCE_WINDOW_S = 14.0
CADENCE_EDGES = 4
FAST_GAP_S = 1.2
SLOW_GAP_S = 4.0


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
    if minutes < 60:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    # Hours, because this is used for how long a pit has been quiet as well as
    # for how long a pump has run. Six hours of silence said "360 min" here and
    # "0.1 h" where the rule formatted its own, and neither is how anybody says
    # it. Seconds are dropped once there are hours: nobody reads them.
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


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
        # One incident, from the first overload until everything is back.
        # Somebody reading a phone wants to know it is over, and a line per
        # pump per event never quite says so. See _incident_is_over.
        self._incident_since: datetime | None = None
        self._incident_trips = 0
        # When it last said the incident was still going.
        self._nagged_at: datetime | None = None
        # When the alarm contact last changed, most recent last, so a flashing
        # alarm can be told from a steady one. See PULSING_EDGES.
        self._alarm_edges: deque[float] = deque(maxlen=32)
        # When the button was last pressed to quiet one.
        self._hushed_at: float | None = None
        # Held, because a task nothing refers to can be collected before it
        # has run.
        self._hushing: asyncio.Task | None = None
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
        # How to press the panel's button, handed in once the broker reader
        # exists. None until then, and None forever on an installation that
        # has not wired a contact across it.
        self.press: Callable[[str], Awaitable[str | None]] | None = None
        # Recoveries in flight, so a sweep that comes round again while one is
        # still holding the button down does not start a second.
        self._recovering: set[int] = set()

    # -- when it runs -------------------------------------------------------

    @property
    def _site_id(self) -> int | None:
        """Which building these rules are about.

        Off the store rather than held, so one place decides. Every query below
        that searches rather than addressing a row by its id carries it: an id
        is unique across the installation, a rule name and a pump number are
        not.
        """
        return self._store.site_id

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

        # After the rules, because whether an incident is over is a question
        # about all of them at once rather than about any one.
        with contextlib.suppress(asyncpg.PostgresError, OSError):
            await self._incident_is_over()
            await self._still_not_right()

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
            INSERT INTO alert (site_id, rule, severity, pump, title, detail, context)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            self._site_id,
            key,
            rule.severity.value,
            pump,
            spec.title,
            detail,
            _as_json(values),
        )
        # Nothing came back, so this alert was already open. Usually that is
        # the condition still being true, which is not news.
        if alert_id is None:
            await self._changed_while_open(key, rule, pump, spec, values, detail)
            return

        log.warning("ALERT %s: %s", key, detail)
        await self._notify(alert_id, "raised", rule, detail)

        # An incident opens on the first overload and counts the rest. Both
        # pumps going is one incident, not two, which is how somebody reading
        # it afterwards thinks of it.
        if key == "overload":
            if self._incident_since is None:
                self._incident_since = datetime.now(UTC)
                self._incident_trips = 0
            self._incident_trips += 1

    async def _changed_while_open(self, key, rule, pump, spec, values, detail) -> None:
        """An open alert whose subject has changed, which is different news.

        Only the values a rule names in `renotify_on`, because almost every
        detail here moves on every sweep: the time is in most of them, the amps
        in others, and how many hours it has been in the rest. Comparing the
        whole sentence would re-announce everything, forever.

        The one that needs it is the device alert, whose subject is a list. It
        opened about the Meter at 06:13 on 2026-09-16 and the X-408 dropped off
        the network at 06:48, which changed the sentence from one name to two.
        The insert conflicted, nothing was written, and nobody was ever told
        the thing that reads the panel had gone. The dashboard showed it,
        because the dashboard reads the devices directly, so the one part of
        the system whose job is to speak up was the part that stayed quiet.
        """
        if not spec.renotify_on:
            return

        row = await self._pool.fetchrow(
            "SELECT id, context FROM alert WHERE site_id = $1 AND rule = $2"
            " AND pump IS NOT DISTINCT FROM $3 AND cleared_at IS NULL",
            self._site_id,
            key,
            pump,
        )
        if row is None:  # pragma: no cover -- cleared between the insert and here
            return

        with contextlib.suppress(ValueError, TypeError):
            before = json.loads(row["context"]) if isinstance(row["context"], str) else {}
            if all(before.get(name) == values.get(name) for name in spec.renotify_on):
                return

        await self._pool.execute(
            "UPDATE alert SET detail = $2, context = $3::jsonb WHERE id = $1",
            row["id"],
            detail,
            _as_json(values),
        )
        log.warning("ALERT %s changed: %s", key, detail)
        await self._notify(row["id"], "raised", rule, detail)

    async def _clear(self, key: str, rule, pump: int | None) -> None:
        row = await self._pool.fetchrow(
            """
            UPDATE alert SET cleared_at = now()
            WHERE site_id = $1 AND rule = $2 AND pump IS NOT DISTINCT FROM $3
              AND cleared_at IS NULL
            RETURNING id, detail
            """,
            self._site_id,
            key,
            pump,
        )
        if row is None:
            return

        log.info("Cleared %s", key)

        # Before the setting below, because putting the pump back is not a
        # kind of message. Hung off the notification, turning off "tell me
        # when it clears" quietly turned off the recovery with it, which is a
        # setting about words disabling a thing that presses a button.
        if key == "overload" and pump:
            self._recover(pump, "reset")

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
                values["recovery"] = await self._recovery_note(pump, "reset")
            else:
                values["state"] = self._who_is_out()
                values["recovery"] = await self._both_note()
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

    def _cover(self, pump: int, moment: str = "reset") -> str:
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
        if moment == "tripped":
            return f"{name} is covering."
        return f"{name} is covering on its own until then."

    def _who_is_out(self) -> str:
        """Which pumps are out, said plainly.

        The both out alert used to clear with "a pump is back... the other one
        may still be out", which is a hedge in a text message about a sewage
        ejector. We know which, so it says which.
        """
        names = self._store.pumps.by_number
        out = [number for number in (1, 2) if self._contact(f"pump{number}_fault")]
        if not out:
            return "Both pumps are back"
        # This rule only clears when one of the two is back, so there is
        # exactly one left to name.
        still = out[0]
        back = 2 if still == 1 else 1
        return f"{names[back].name} is back and {names[still].name} is still out"

    # -- pressing the button without being asked -----------------------------
    #
    # Two moments, one per pump. When an overload trips, silence the horn.
    # When the relay clears, reset the panel, which is the press that actually
    # puts the pump back in the rotation: the fault going away does not.
    #
    # In hand reset mode this cannot run away, because the relay stays tripped
    # until somebody presses it and nothing here can do that. In auto reset
    # mode it is a loop, and a loop around a motor that overloads because
    # something is wrong with it is how one pump out of service becomes two
    # burned out. Hence the count, and hence it stopping rather than warning.

    async def _trips(self, pump: int) -> int:
        """How many times this pump has tripped inside the window."""
        button = self._store.panel_button
        return (
            await self._pool.fetchval(
                """
                SELECT count(*) FROM alert
                WHERE site_id = $1 AND rule = 'overload' AND pump = $2
                  AND raised_at > now() - ($3::int * interval '1 minute')
                """,
                self._site_id,
                pump,
                button.within_minutes,
            )
            or 0
        )

    async def _recovery_note(self, pump: int, moment: str) -> str:
        """What to tell somebody is being done about it, in one sentence.

        Written in the present rather than the past on purpose: the message
        goes out the instant the contact moves, and the button has not been
        pressed yet when it does.
        """
        button = self._store.panel_button
        hushed = "The alarm has been silenced. " if button.silencing else ""

        if not button.recovering:
            # Nothing is going to clear it, so say so. With recovery on this
            # would not merely be wordy, it would be wrong: somebody reading
            # "you have to go and press it" while the panel is already
            # clearing itself drives to the building for nothing.
            if moment == "both":
                return f"{hushed}Nobody is clearing this. Get to the panel."
            if moment == "tripped":
                return f"{hushed}Reset the overload, then clear the alarm at the panel."
            return f"{hushed}Clear the alarm at the panel to put it back in service."

        # This trip is not on the record yet when the message for it is built:
        # the check runs before the row is inserted. Counting it here is what
        # makes the sentence and the decision agree.
        trips = await self._trips(pump) + (1 if moment == "tripped" else 0)
        if button.max_trips and trips > button.max_trips:
            return (
                f"{hushed}That is {trips} trips in {button.within_minutes} "
                "minutes, so automatic recovery has stopped. The pump is out "
                "until somebody looks at it."
            )
        if moment == "tripped":
            # Written for the reference panel, whose relays are set to auto
            # reset: the bimetal cools, the relay comes back on its own, and
            # this clears the panel behind it. Anybody running hand reset
            # relays should change this wording on the alerts page, because
            # nothing here can see which way the dial is set.
            return (
                f"{hushed}The overload should reset itself once it has cooled "
                "and the panel will clear itself after it."
            )
        if moment == "both":
            return (
                f"{hushed}Trying to clear both. If this has not sorted itself "
                "out in a few minutes, somebody needs to get to the building."
            )
        return "Clearing the panel alarm now to bring it back into rotation."

    def _alarm_is_pulsing(self) -> bool:
        """Whether the panel is still asking to be acknowledged.

        Read off how often the contact has changed rather than what it reads
        this instant, because half of a flashing alarm is indistinguishable
        from no alarm at all.
        """
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover -- called outside a loop
            return False
        recent = [at for at in self._alarm_edges if now - at <= PULSING_WITHIN_S]
        return len(recent) >= PULSING_EDGES

    def _alarm_cadence(self) -> str:
        """What the alarm output is doing: "fast", "slow" or "steady".

        Measured from the edges rather than read off the contact, for the same
        reason the dashboard lamp is: a dropped frame looks exactly like a
        pulse that stopped, and the difference between these two patterns is
        the difference between a flood and a postcard.
        """
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:  # pragma: no cover -- called outside a loop
            return "steady"
        recent = [at for at in self._alarm_edges if now - at <= CADENCE_WINDOW_S]
        if len(recent) < CADENCE_EDGES:
            return "steady"
        gaps = sorted(b - a for a, b in itertools.pairwise(recent))
        middle = gaps[len(gaps) // 2]
        if middle <= FAST_GAP_S:
            return "fast"
        if middle <= SLOW_GAP_S:
            return "slow"
        return "steady"

    def _silence_the_alarm(self) -> None:
        """Stop the horn, while it is still asking to be stopped.

        Answering the flashing directly is the whole of it. Everything before
        this tried to work out from the faults whether an alarm was new, and
        kept being wrong about it, because this panel raises a fresh alarm
        every time one is cleared while a pump is still out: three of them in
        one incident on 2026-09-13, each looking exactly like the last.

        The flashing does not care which alarm it is or what caused it. It
        means the panel has not been acknowledged, which is the only question
        worth asking, and it retries itself: a press that does not land leaves
        it flashing and the next edge asks again.

        Silencing hides nothing. The alarm stays raised, the alert stays open,
        the message still goes out. This only stops the noise, which is why it
        does not care about the give up limit either.
        """
        if self.press is None or not self._store.panel_button.silencing:
            return
        if self._hushing is not None and not self._hushing.done():
            return
        if not self._alarm_is_pulsing():
            return

        now = asyncio.get_running_loop().time()
        if self._hushed_at is not None and now - self._hushed_at < HUSH_AGAIN_AFTER_S:
            return
        self._hushed_at = now

        async def hush() -> None:
            try:
                # A reset is holding the button, or about to, and that clears
                # the alarm outright. Silencing first would be pressing twice
                # to do less, and the press would land after the alarm had
                # gone: a tap into a quiet panel is how the controller runs
                # its lamp test, so it would raise one rather than end one.
                if self._recovering:
                    return
                await self._press("silence", "the panel")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Silencing the panel raised")

        # Held, because a task nothing refers to can be collected before it
        # has run.
        self._hushing = asyncio.create_task(hush())

    def _recover(self, pump: int, moment: str) -> None:
        """Start a recovery, off the sweep.

        A press holds the contact closed for seconds and the sweep must not
        wait on it: the same sweep is what notices the other pump.
        """
        if self.press is None or not self._store.panel_button.recovering:
            return
        if pump in self._recovering:
            return
        self._recovering.add(pump)
        task = asyncio.create_task(self._do_recover(pump, moment))
        task.add_done_callback(lambda _: self._recovering.discard(pump))

    async def _do_recover(self, pump: int, moment: str) -> None:
        button = self._store.panel_button
        name = self._store.pumps.by_number[pump].name
        try:
            # A limit of zero is no limit: keep clearing it however often it
            # happens.
            if button.max_trips and await self._trips(pump) > button.max_trips:
                log.warning("Not recovering %s: too many trips", name)
                return

            # Cleared as soon as any pump can come back, not once every fault
            # has gone.
            #
            # Waiting for both was written here on 2026-09-13 and was exactly
            # backwards. The latched alarm is what holds a recovered pump out
            # of the rotation: measured the same day, a relay reset at 14:48:52
            # and the pump did not run again until 17:29:56, after the alarm
            # was cleared at 17:25:03. So holding the alarm up until the second
            # pump is fixed does not keep anything safe, it turns one working
            # pump into none.
            #
            # The pump that is still faulted rejoins, gets a call, trips again
            # and is silenced again. That is churn, not danger: the good pump
            # keeps pumping throughout, and each retrip counts toward the limit
            # that stops this happening forever.
            await self._press("reset", name)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("A recovery raised while pressing the panel button")

    async def _press(self, action: str, name: str) -> None:
        if self.press is None:
            return
        failed = await self.press(action)
        if failed:
            log.warning("Could not %s the panel for %s: %s", action, name, failed)
        else:
            log.info("Pressed %s on the panel for %s", action, name)

    def _where_things_stand(self) -> str:
        """What is wrong, in the order somebody would want to hear it.

        Written for a phone in the middle of an incident, so it leads with
        whether anything is pumping. Everything else is detail behind that.
        """
        names = self._store.pumps.by_number
        out = [names[pump].name for pump in (1, 2) if self._contact(f"pump{pump}_fault")]
        if len(out) == 2:
            said = "Both pumps are out on overload and nothing is pumping."
        elif out:
            left = names[2 if out[0] == names[1].name else 1].name
            said = f"{out[0]} is out on overload and {left} is covering on its own."
        else:
            said = "Both pumps are available."

        if self._panel_alert_since is not None:
            said += " The panel alarm is still up."
        return said

    async def _still_not_right(self) -> None:
        """Say it is still not right, at intervals, while it still is not.

        The rules speak when things change, which leaves the middle of a long
        incident silent, and silence reads the same as fixed. Nothing said
        anything for twenty minutes on 2026-09-13 while a pump sat out and the
        alarm sounded, because in those twenty minutes nothing had changed:
        not being over is not an event, and it is the thing somebody wants to
        know about.
        """
        every = self._store.alerts.unresolved_every_minutes
        if not every or self._incident_since is None:
            return
        now = datetime.now(UTC)
        since = self._nagged_at or self._incident_since
        if (now - since).total_seconds() < every * 60:
            return
        self._nagged_at = now

        went_on = round((now - self._incident_since).total_seconds() / 60)
        where = self._store.site.where or "the pit"
        await dispatch.tell(
            self._pool,
            self._store,
            message=(
                f"Still not right at {where}, {went_on} minutes on. {self._where_things_stand()}"
            ),
            severity=Severity.CRITICAL,
            event="recovered",
        )

    async def _incident_is_over(self) -> None:
        """One message saying it is finished, when it is finished.

        The alerts say what went wrong and what stopped being wrong, a line per
        pump per event, and on 2026-09-13 that was eight of them for a single
        incident. Every one was true and none of them said the thing somebody
        scrolling a phone actually wants, which is whether it is over and
        whether they still have to do anything. The last message in that chain
        was about one pump.

        So the incident is a thing in its own right. It opens on the first
        overload and closes when there is nothing out and nothing sounding,
        and this is the only message that speaks for the whole of it.
        """
        if self._incident_since is None:
            return
        # Anything still out, or an alarm still up, and it is not over. The
        # alarm is read through _panel_alert_since rather than off the contact,
        # so that the dark half of a flash does not look like quiet.
        if self._faults_out() or self._panel_alert_since is not None:
            return
        if await self._pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM alert WHERE site_id = $1 AND cleared_at IS NULL "
            "AND severity IN ('warning', 'critical'))",
            self._site_id,
        ):
            return

        trips = self._incident_trips
        went_on = datetime.now(UTC) - self._incident_since
        minutes = max(1, round(went_on.total_seconds() / 60))
        self._incident_since = None
        self._incident_trips = 0
        self._nagged_at = None

        where = self._store.site.where or "the pit"
        said = f"All clear at {where}. Both pumps are in the rotation and the alarm is off."
        if trips > 1:
            said += (
                f" That was {trips} overloads in {minutes} "
                f"{'minute' if minutes == 1 else 'minutes'}, which is worth "
                "having the pumps looked at."
            )
        log.info("Incident over after %d overload(s)", trips)
        # A warning rather than news, because somebody who only asked to hear
        # about problems still needs the one that says the problem is done.
        await dispatch.tell(
            self._pool,
            self._store,
            message=said,
            severity=Severity.WARNING,
            event="recovered",
        )

    def _faults_out(self) -> int:
        """How many pumps are out on their own overload right now."""
        return sum(1 for pump in (1, 2) if self._contact(f"pump{pump}_fault"))

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
                Finding(
                    pump=pump,
                    values={
                        "overload": self._overload_label(pump),
                        "cover": self._cover(pump, "tripped"),
                        "recovery": await self._recovery_note(pump, "tripped"),
                    },
                )
                if tripped
                else None
            )
        return found or None

    async def _both_note(self) -> str:
        """The same sentence for the rule that has no pump behind it.

        Worked out per pump, because the count that decides whether recovery
        has given up is per pump and both of them being out is not a third
        pump. If either has given up then nothing is going to clear itself,
        which is the half that has to reach somebody.
        """
        note = await self._recovery_note(1, "both")
        if self._store.panel_button.recovering:
            other = await self._recovery_note(2, "both")
            if "stopped" in other:
                return other
        return note

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
        if not (one and two):
            return {None: None}
        return {None: Finding(values={"recovery": await self._both_note()})}

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
                        WHERE site_id = $1 AND cleared_at IS NULL AND rule <> 'panel_alert'
                          AND severity IN ('warning', 'critical')
                    )
                    """,
                    self._site_id,
                )
            )
        if self._panel_alert_explained:
            return {None: None}
        return {None: Finding(values={"pattern": self._pattern_note()})}

    def _pattern_note(self) -> str:
        """What the alarm looks like, said plainly, because on this panel the
        rate is the only thing that carries any meaning at all.

        Before this the message offered one guess, a power failure or an open
        door, whatever the contact was doing. On 2026-09-16 that guess went out
        about a yearly service reminder and sent somebody looking for a fault
        that did not exist.
        """
        cadence = self._alarm_cadence()
        if cadence == "fast":
            return "It is flashing, which means nobody has acknowledged it yet."
        if cadence == "slow":
            return (
                "It is pulsing slowly, about five seconds. On this controller "
                "that is the yearly service reminder rather than a fault, and "
                "it is cleared at the panel."
            )
        return "It is steady, which is often a power failure or the panel door left open."

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
        alarm = inputs.channel_for("system_alert")
        for event in events:
            # Both edges of the alarm, before anything else looks at them.
            # Which way it went does not matter: what says the panel is still
            # asking is that it keeps going both ways. See PULSING_EDGES.
            if alarm and event.channel == alarm:
                with contextlib.suppress(RuntimeError):
                    self._alarm_edges.append(asyncio.get_running_loop().time())
                self._silence_the_alarm()
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
            INSERT INTO alert (site_id, rule, severity, title, detail, context, cleared_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, now())
            RETURNING id
            """,
            self._site_id,
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
            FROM pump_run WHERE site_id = $1 AND ended_at IS NULL
            """,
            self._site_id,
        )
        running = {row["pump"]: row["running_s"] for row in rows}
        found = {}
        for pump in (1, 2):
            seconds = running.get(pump)
            over = seconds is not None and seconds * 1000 >= rule.longer_than_ms
            if seconds is not None and not over:
                # Come back when this run crosses the line, rather than at the
                # next tick. A sweep happens when a contact changes and every
                # thirty seconds otherwise, and both ends of a run are contact
                # changes, so a run shorter than a tick was only ever looked at
                # at the moment it started and the moment it finished. On the
                # reference pit, which runs for twelve seconds, that meant no
                # threshold under thirty could fire at all, and the sixty
                # second one was reported up to a tick late.
                # float, because extract(epoch) comes back as a Decimal and
                # mixing the two raises rather than converting.
                self._ask_again_in(rule.longer_than_ms / 1000 - float(seconds))
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
        last = await self._pool.fetchval(
            "SELECT max(started_at) FROM pump_run WHERE site_id = $1", self._site_id
        )
        if last is None:
            # Nothing has ever run, which on a fresh install is not news.
            return None
        quiet_s = (datetime.now(UTC) - last).total_seconds()
        over = quiet_s / 60 >= rule.quiet_minutes
        # Spelled rather than given as a decimal fraction of an hour. Six
        # minutes of quiet went out reading "for 0.1 h", which is a number
        # nobody says out loud and, on a rule about a pit nobody is watching,
        # reads like the monitor is the thing that is broken.
        return {None: Finding(values={"quiet": _spell(quiet_s)}) if over else None}

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
            "SELECT pump, max(started_at) AS last FROM pump_run WHERE site_id = $1 GROUP BY pump",
            self._site_id,
        )
        last = {row["pump"]: row["last"] for row in rows}
        # The oldest run on record, which is how far back the evidence goes.
        watching_since = await self._pool.fetchval(
            "SELECT min(started_at) FROM pump_run WHERE site_id = $1", self._site_id
        )
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
        """The pit refilling the moment it is emptied, counted across both
        pumps rather than per pump.

        It used to measure each pump against its own previous run, and that is
        the wrong interval for the fault it looks for. A check valve that has
        stopped sealing lets the column of water in the discharge pipe run back
        into the pit, the float closes again, and a duplex panel answers by
        calling **the other pump**, because it alternates. So the pit's own
        rhythm is short while neither pump's individual rhythm is: each one's
        gap spans the other one's whole run plus both intervals, which is
        roughly double, and on the reference pit that was the difference
        between 8 s and 44 s against a 45 s threshold.

        Per pit is also the honest reading of the sentence. It says the pumps
        are restarting within so long of stopping, which is a statement about
        the pit, and it is the pit that the check valve is filling.
        """
        if not rule.restart_within_ms:
            return None
        gaps = await self._pool.fetch(
            """
            SELECT extract(epoch FROM started_at - lag(ended_at)
                   OVER (ORDER BY started_at)) AS gap_s
            FROM pump_run
            WHERE site_id = $1 AND started_at > now() - interval '2 hours'
            ORDER BY started_at DESC LIMIT $2
            """,
            self._site_id,
            rule.times_in_a_row,
        )
        measured = [float(row["gap_s"]) for row in gaps if row["gap_s"] is not None]
        tight = len(measured) >= rule.times_in_a_row and all(
            gap * 1000 <= rule.restart_within_ms for gap in measured
        )
        if not tight:
            return {None: None}
        return {
            None: Finding(
                values={"gap": _spell(max(measured)), "times": rule.times_in_a_row},
            )
        }

    # -- what the clamps say ------------------------------------------------

    async def _check_contactor_no_current(self, rule) -> dict | None:
        """A closed contactor drawing nothing: a motor that is not turning.

        The reason for having both a contact and a clamp, and the one rule that
        needs them to disagree. It stays quiet unless the clamp has proved it
        can see current at some point, because a channel that has never read
        above the running threshold is far more likely to be a CT nobody fitted
        than a pump that has never once worked, and firing on an unfitted CT
        every time the pump runs is how somebody learns to ignore this.

        **And it stays quiet when the clamp has stopped talking**, which is the
        same argument and was only half implemented. The live reading is a
        cache of the last thing the meter said, with no expiry: when the meter
        went off the broker on 2026-09-16 that cache held its final between
        runs zero, and every pump call afterwards looked like a motor sitting
        dead on a closed contactor. Fifteen critical alerts in an hour, about
        two pumps that were running perfectly.

        A monitoring failure reported as a hardware failure is worse than no
        alert at all, because somebody acts on it. So the clamp has to have
        spoken *during this run* to have an opinion about it. That needs no
        timeout to tune: the meter is asked every second while a pump turns, so
        a run with nothing from the clamp is a clamp that is not answering, and
        the device offline rule is the one that should be speaking then.
        """
        found = {}
        for pump in (1, 2):
            running = self._contact(f"pump{pump}_run")
            channel = self._store.mqtt.clamp_for_pump.get(pump)
            if running is None or channel is None:
                continue
            proven = await self._pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM em_sample"
                " WHERE site_id = $1 AND channel = $2 AND current >= $3)",
                self._site_id,
                channel,
                domain.RUNNING_AMPS,
            )
            if not proven:
                continue

            run = await self._pool.fetchrow(
                "SELECT started_at, extract(epoch FROM now() - started_at) AS open_s"
                " FROM pump_run WHERE site_id = $1 AND pump = $2 AND ended_at IS NULL",
                self._site_id,
                pump,
            )
            sample = self._live.samples.get(channel)
            heard_this_run = (
                sample is not None and run is not None and sample.ts >= run["started_at"]
            )
            if running and run is not None and run["open_s"] >= SETTLE_S and not heard_this_run:
                # The contact says it is turning and the clamp has said nothing
                # since before it started. That is a meter that has stopped, not
                # a motor that has.
                found[pump] = None
                continue

            amps = sample.current if sample else None
            dead = (
                running
                and run is not None
                and run["open_s"] >= SETTLE_S
                and heard_this_run
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
                "SELECT current FROM em_sample"
                " WHERE site_id = $1 AND channel = $2 AND current IS NOT NULL"
                " ORDER BY ts DESC LIMIT $3",
                self._site_id,
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
            typical = await self._history.typical(
                self._pool, self._site_id, channel, domain.RUNNING_AMPS
            )
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
            recent = await self._recent.from_contacts(
                self._pool, self._site_id, pump, self._store.site.timezone
            )
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
        rows = await self._pool.fetch(
            "SELECT device, online FROM device_status WHERE site_id = $1", self._site_id
        )
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
