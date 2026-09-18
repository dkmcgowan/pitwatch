"""Page routes: setup, sign in, settings.

The dashboard is not here. It reads live data and lives in its own module.

One decision worth stating: the setup page is reachable without signing in, but
only while there is no account. Once one exists, the route redirects. That is
what makes the first boot usable at all without shipping a default password,
and it closes as soon as it has been used. Anyone who can reach the port during
that window can claim the install, which is the same window every appliance
has, and is why the README does not suggest putting this on the internet.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from pitwatch import auth, clock
from pitwatch import summary as summaries
from pitwatch.api import forms
from pitwatch.domain import alerts as alert_specs
from pitwatch.domain import diagnostics, series, sites
from pitwatch.notify import email as email_sender
from pitwatch.notify import sms as sms_sender
from pitwatch.schemas import DASHBOARD_ROLES, SCHEDULE_CHOICES, SUMMARY_WINDOWS
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

router = APIRouter()


def _context(request: Request, **extra) -> dict:
    store: SettingsStore = sites.store_for(request)
    return {
        "site": store.site,
        "user": auth.current_user(request),
        **sites.switcher(request),
        # The eight things the dashboard can draw, which is the list the input
        # rows pick from. Here rather than passed by each caller, because every
        # page that renders those rows needs it and forgetting it renders eight
        # empty dropdowns rather than an error. The same argument for the two
        # below: a dropdown with no options posts nothing and looks fine.
        "roles": DASHBOARD_ROLES,
        # Not every window the history page draws: a summary reads a week or a
        # month, because a model handed one day has nothing to compare it to.
        "windows": [(key, series.WINDOWS[key].title) for key in SUMMARY_WINDOWS],
        "schedule_choices": SCHEDULE_CHOICES,
        **extra,
    }


def _templates(request: Request):
    return request.app.state.templates


# -- setup ------------------------------------------------------------------


@router.get("/setup", include_in_schema=False)
async def setup_page(request: Request, owner: auth.IsOwner):
    store: SettingsStore = sites.store_for(request)
    if await store.is_setup_complete():
        return RedirectResponse("/settings", status_code=303)
    return _templates(request).TemplateResponse(
        request,
        "setup.html",
        _context(
            request,
            mqtt=store.mqtt,
            pumps=store.pumps,
            error=None,
        ),
    )


@router.post("/setup", include_in_schema=False)
async def setup_submit(request: Request, owner: auth.IsOwner):
    store: SettingsStore = sites.store_for(request)
    form = await request.form()

    try:
        site = forms.site_from(form)
        mqtt = forms.mqtt_from(form, store.mqtt)
        pumps = forms.pumps_from(form)
    except (ValueError, ValidationError) as error:
        return _templates(request).TemplateResponse(
            request,
            "setup.html",
            _context(
                request,
                mqtt=store.mqtt,
                pumps=store.pumps,
                error=_readable(error),
            ),
            status_code=400,
        )

    for value in (site, mqtt, pumps):
        await store.put(value)
    await _rename_site(request, store, site)

    # The person doing the setup is the first person alerts should reach, and
    # asking again on a profile page afterwards is asking twice.
    await _save_own_details(request, owner, form)

    await store.mark_setup_complete()
    log.info("Setup completed by %s", owner.username)
    return RedirectResponse("/", status_code=303)


async def _rename_site(request: Request, store: SettingsStore, settings) -> None:
    """Keep the `site` row's name in step with the settings.

    The name lives in two places and has to agree in both: the settings are
    what the pages print, and the `site` row is what the switcher lists and
    what orders it. A building named on setup but left as "This site" in the
    table is one name in the header and another on the page.
    """
    if store.site_id is None or not settings.name.strip():
        return
    await request.app.state.pool.execute(
        "UPDATE site SET name = $2 WHERE id = $1", store.site_id, settings.name.strip()[:120]
    )


# -- settings ---------------------------------------------------------------


@router.get("/settings", include_in_schema=False)
async def settings_page(
    request: Request, owner: auth.IsOwner, saved: str | None = None, error: str | None = None
):
    store: SettingsStore = sites.store_for(request)
    return _templates(request).TemplateResponse(
        request,
        "settings.html",
        _context(
            request,
            # Every building, not only the ones the header would offer. The
            # switcher hides itself when there is one; this list is how a
            # second one gets made, so it has to show the first.
            all_sites=await sites.all_sites(request.app.state.pool),
            mqtt=store.mqtt,
            pumps=store.pumps,
            smtp=store.smtp,
            sms=store.sms,
            summary=store.summary,
            ai=store.ai,
            weather=store.weather,
            tide=store.tide,
            groundwater=store.groundwater,
            panel_button=store.panel_button,
            diagnostics=await diagnostics.read(
                request.app.state.pool, store.site_id, store.mqtt, store.pumps, store.site
            ),
            saved=saved,
            error=error,
        ),
    )


# -- alerts ------------------------------------------------------------------
#
# Its own place in the header rather than a room off the settings page. A dozen
# rules each carrying a level, an audience, a message and sometimes a threshold
# is not a section of anything, and a settings page whose job is to send you
# somewhere else is a menu pretending to be a page.
#
# It owns every threshold that raises an alert. A number on the pumps page
# tells you nothing about what happens when it is crossed; the same number
# beside its own message tells you everything.


def _local(when, zone: str) -> str:
    """A timestamp in the building's own clock. See pitwatch.clock."""
    return clock.on_at(when, zone)


def _spoken(seconds: float | None) -> str:
    """How long something lasted, in the units somebody would say it in."""
    if seconds is None or seconds < 1:
        return ""
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172_800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86_400:.0f} d"


@router.get("/alerts", include_in_schema=False)
async def alert_history(request: Request, user: auth.SignedIn):
    """What has happened, which is what somebody opening the bell wants.

    The rules are a tab away rather than a header icon of their own: they are
    set once and revisited when one turns out wrong, which is rarer than
    wanting to know whether the pit did anything last night.
    """
    pool = request.app.state.pool
    site_id = sites.store_for(request).site_id
    zone = sites.store_for(request).site.timezone

    def shape(row, *, ended):
        lasted = row["cleared_at"] - row["raised_at"] if ended else None
        return {
            "severity": row["severity"],
            "title": row["title"],
            "detail": row["detail"],
            "raised_local": _local(row["raised_at"], zone),
            "lasted": _spoken(lasted.total_seconds() if lasted else None) or "open",
            "bad": not ended,
        }

    rows = await pool.fetch(
        """
        SELECT severity, title, detail, raised_at, cleared_at
        FROM alert WHERE site_id = $1 ORDER BY raised_at DESC LIMIT 200
        """,
        site_id,
    )
    sent = await pool.fetch(
        """
        SELECT n.channel, n.target, n.status, n.error, n.created_at, a.detail
        FROM notification n LEFT JOIN alert a ON a.id = n.alert_id
        WHERE n.site_id = $1
        ORDER BY n.created_at DESC LIMIT 50
        """,
        site_id,
    )
    return _templates(request).TemplateResponse(
        request,
        "alert_history.html",
        _context(
            request,
            tab="history",
            open=[shape(row, ended=False) for row in rows if row["cleared_at"] is None],
            past=[shape(row, ended=True) for row in rows if row["cleared_at"] is not None],
            messages=[
                {
                    "channel": row["channel"],
                    "target": row["target"],
                    "status": row["status"],
                    "error": row["error"],
                    "detail": row["detail"] or "",
                    "when_local": _local(row["created_at"], zone),
                }
                for row in sent
            ],
        ),
    )


@router.get("/alerts/settings", include_in_schema=False)
async def alerts_page(request: Request, admin: auth.IsAdmin, saved: str | None = None):
    store: SettingsStore = sites.store_for(request)
    return _templates(request).TemplateResponse(
        request,
        "alerts.html",
        _context(
            request,
            tab="settings",
            specs=alert_specs.SPECS,
            rules=store.alerts.by_key,
            alerts=store.alerts,
            saved=saved is not None,
            error=None,
        ),
    )


@router.post("/alerts/settings", include_in_schema=False)
async def alerts_save(request: Request, admin: auth.IsAdmin):
    store: SettingsStore = sites.store_for(request)
    form = await request.form()
    try:
        await store.put(forms.alerts_from(form, store.alerts))
    except (ValueError, ValidationError) as error:
        return _templates(request).TemplateResponse(
            request,
            "alerts.html",
            _context(
                request,
                tab="settings",
                specs=alert_specs.SPECS,
                rules=store.alerts.by_key,
                alerts=store.alerts,
                saved=False,
                error=_readable(error),
            ),
            status_code=400,
        )
    log.info("%s updated the alert rules", admin.username)
    return RedirectResponse("/alerts/settings?saved=1", status_code=303)


# -- history and the written summary -----------------------------------------
#
# History is for everybody. It is the same data the dashboard shows, over time,
# and there is nothing on it somebody who can read the dashboard should not
# see.
#
# The summary is not. Writing one spends money on an OpenAI account and hands
# a description of the building to somebody else's model, and both of those are
# the owner's decision rather than a page anybody signed in can press.


@router.get("/history", include_in_schema=False)
async def history_page(request: Request, user: auth.SignedIn):
    return _templates(request).TemplateResponse(request, "history.html", _context(request))


# Not an administrator's page. It was one because every run spends money on an
# OpenAI account, and that stopped being the deciding fact once this could be
# pointed at a model running on the same network.
@router.get("/summary", include_in_schema=False)
async def summary_page(request: Request, user: auth.SignedIn, error: str | None = None):
    store: SettingsStore = sites.store_for(request)
    last = await summaries.latest(request.app.state.pool, store.site_id)
    return _templates(request).TemplateResponse(
        request,
        "summary.html",
        _context(
            request,
            last=last,
            age=summaries.age(last["created_at"]) if last else "",
            # The window it read, in words. The key is what is stored, because
            # a stored label is a label that goes stale the day one is renamed.
            read_over=_read_over(last["window_key"]) if last else "",
            # The one it last read, so pressing again repeats rather than
            # silently going back to a week.
            chosen=(last["window_key"] if last else summaries.WINDOW.key),
            # Whether PitWatch has an account to ask through at all, which
            # is not a fact about this building.
            ready=store.ai.ready,
            error=error,
        ),
    )


def _read_over(key: str) -> str:
    """What the summary read, as the end of "written from ...".

    "7 days of readings" and "30 days of readings" both work off the title;
    "today of readings" does not, which is what comes out of assuming the three
    labels are the same part of speech.
    """
    window = series.WINDOWS.get(key)
    if window is None:
        return key
    return "today's readings" if window.from_midnight else f"{window.title} of readings"


@router.post("/summary", include_in_schema=False)
async def summary_write(request: Request, user: auth.SignedIn):
    form = await request.form()
    store: SettingsStore = sites.store_for(request)
    # Today is resolved against the building's clock here, the same as it is for
    # the history page, so the two mean the same day.
    asked = str(form.get("window") or "")
    window = series.window_for(
        asked if asked in SUMMARY_WINDOWS else summaries.WINDOW.key, store.site.timezone
    )
    try:
        await summaries.write(request.app, store, user.username, window)
    except summaries.SummaryError as error:
        # Straight back to the page with what went wrong on it. The one thing
        # somebody needs after a failed call is the reason, and the model's own
        # message is nearly always the reason.
        return RedirectResponse(f"/summary?error={quote(str(error)[:300])}", status_code=303)
    return RedirectResponse("/summary", status_code=303)


@router.post("/settings/{section}", include_in_schema=False)
async def settings_save(request: Request, section: str, owner: auth.IsOwner) -> HTMLResponse:
    store: SettingsStore = sites.store_for(request)
    form = await request.form()

    try:
        match section:
            case "site":
                settings = forms.site_from(form)
                await store.put(settings)
                await _rename_site(request, store, settings)
            case "mqtt":
                await store.put(forms.mqtt_from(form, store.mqtt))
            case "pumps":
                await store.put(forms.pumps_from(form))
            case "smtp":
                await store.put(forms.smtp_from(form, store.smtp))
            case "sms":
                await store.put(forms.sms_from(form, store.sms))
            case "summary":
                await store.put(forms.summary_from(form, store.summary))
            # PitWatch's own account rather than this building's schedule, and
            # a separate form for that reason. One key serves every building.
            case "ai":
                await store.put(forms.ai_from(form, store.ai))
            case "weather":
                await store.put(forms.weather_from(form))
            case "tide":
                await store.put(forms.tide_from(form))
            case "groundwater":
                await store.put(forms.groundwater_from(form))
            case "panel_button":
                await store.put(forms.panel_button_from(form))
            case _:
                return RedirectResponse("/settings", status_code=303)
    except (ValueError, ValidationError) as error:
        return _templates(request).TemplateResponse(
            request,
            "settings.html",
            _context(
                request,
                all_sites=await sites.all_sites(request.app.state.pool),
                mqtt=store.mqtt,
                pumps=store.pumps,
                smtp=store.smtp,
                sms=store.sms,
                summary=store.summary,
                ai=store.ai,
                weather=store.weather,
                tide=store.tide,
                groundwater=store.groundwater,
                panel_button=store.panel_button,
                diagnostics=await diagnostics.read(
                    request.app.state.pool, store.site_id, store.mqtt, store.pumps, store.site
                ),
                saved=None,
                error=_readable(error),
            ),
            status_code=400,
        )

    log.info("%s saved the %s settings", owner.username, section)
    return RedirectResponse(f"/settings?saved={section}", status_code=303)


async def _save_own_details(request: Request, owner: auth.User, form) -> None:
    """Record the administrator's own contact details from the setup form.

    Quietly skipped if what was typed does not make sense, because failing the
    whole of setup over a mistyped phone number would be a poor trade. The
    profile page says so properly.
    """
    email = forms.text(form, "admin_email") or None
    phone = forms.text(form, "admin_phone") or None
    if phone:
        phone = sms_sender.normalize(phone)
        if not sms_sender.looks_like_a_number(phone):
            log.warning("Ignoring an unusable phone number from setup")
            phone = None
    if email and not email_sender.looks_like_an_address(email):
        log.warning("Ignoring an unusable email address from setup")
        email = None

    await request.app.state.pool.execute(
        """
        UPDATE app_user
        SET name = COALESCE(NULLIF($2, ''), name),
            email = COALESCE($3, email),
            phone = COALESCE($4, phone),
            notify_email = $5,
            notify_sms = $6
        WHERE id = $1
        """,
        owner.id,
        forms.text(form, "admin_name"),
        email,
        phone,
        forms.checkbox(form, "admin_notify_email") and bool(email),
        forms.checkbox(form, "admin_notify_sms") and bool(phone),
    )


def _readable(error: Exception) -> str:
    """One line a person can act on, out of a pydantic error or a plain one."""
    if isinstance(error, ValidationError):
        problems = []
        for detail in error.errors():
            location = ".".join(str(part) for part in detail["loc"]) or "value"
            problems.append(f"{location}: {detail['msg']}")
        return "; ".join(problems[:3])
    return str(error)


# -- more than one building --------------------------------------------------
#
# Both of these answer 404 shaped redirects rather than errors on a single site
# installation, because that is what they are: there is nothing to switch to and
# nothing that would draw the control.


@router.post("/site/switch", include_in_schema=False)
async def switch_site(request: Request, user: auth.SignedIn) -> RedirectResponse:
    """Look at a different building.

    Remembered in the session rather than in the URL, so every page follows
    without carrying a query string, and checked against what this person may
    see rather than trusted: the id arrives from a form, and a form is a thing
    anybody can post.
    """
    form = await request.form()
    try:
        wanted = int(str(form.get("site_id") or ""))
    except ValueError:
        return RedirectResponse("/", status_code=303)

    allowed = await sites.visible_to(request.app.state.pool, user)
    if not any(site.id == wanted for site in allowed):
        log.warning("%s asked for site %d, which is not theirs", user.username, wanted)
        return RedirectResponse("/", status_code=303)

    request.session[sites.SESSION_SITE_KEY] = wanted
    log.info("%s switched to site %d", user.username, wanted)
    # Back where they were, so switching from the history page keeps them on
    # the history page rather than bouncing them to the dashboard.
    return RedirectResponse(_back_to(request), status_code=303)


@router.post("/settings/sites/new", include_in_schema=False)
async def add_site(request: Request, owner: auth.IsOwner) -> RedirectResponse:
    """A second building. Owner only, because it is a PitWatch level act."""
    form = await request.form()
    name = str(form.get("new_site_name") or "").strip()
    if not name:
        return RedirectResponse("/settings?error=A+site+needs+a+name", status_code=303)

    try:
        site_id = await sites.create(request.app.state.pool, name[:120])
    except Exception as error:  # noqa: BLE001 -- shown, not raised
        log.warning("Could not add the site %r: %s", name, error)
        return RedirectResponse(f"/settings?error={quote(str(error)[:200])}", status_code=303)

    log.info("%s added site %d (%s)", owner.username, site_id, name)
    return RedirectResponse("/settings?saved=1", status_code=303)


def _back_to(request: Request) -> str:
    """The page that posted, if it was one of ours. Never somewhere else."""
    referer = request.headers.get("referer") or ""
    path = urlsplit(referer).path or "/"
    return path if path.startswith("/") and not path.startswith("//") else "/"


def register(app) -> None:
    app.include_router(router)


__all__ = ["register", "router"]
