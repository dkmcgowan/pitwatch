"""The FastAPI application and its startup sequence.

Startup order matters and is not obvious, so it is spelled out here rather than
spread across modules: connect to the database, apply migrations, load settings,
seed anything the environment asked for, then start the ingest tasks. Nothing
touches hardware until the settings that say where the hardware is have been
read.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.middleware.sessions import SessionMiddleware

from pitwatch import __version__, auth, csrf
from pitwatch.api import live as live_api
from pitwatch.api import pages, stream, users
from pitwatch.config import Config, get_config
from pitwatch.db import lifespan_pool
from pitwatch.domain.history import CurrentHistory, RecentRuns, SignalHistory
from pitwatch.ingest.sink import LiveIo, LiveState
from pitwatch.ingest.supervisor import Supervisor
from pitwatch.middleware import RequireSignIn, SecurityHeaders
from pitwatch.settings import SettingsStore, seed_from_environment

log = logging.getLogger(__name__)


@pass_context
def _csrf_token(context) -> str:
    """The token for the request being rendered.

    A context function rather than a value, because the token belongs to the
    session and templates are shared across requests.
    """
    request = context.get("request")
    return csrf.token_for(request) if request is not None else ""


PACKAGE_ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))


def configure_logging(config: Config) -> None:
    logging.basicConfig(
        level=config.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # uvicorn logs every request at INFO, which buries anything useful once the
    # dashboard is open and polling nothing but still loading assets.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def create_app(config: Config | None = None, *, secret_key: str | None = None) -> FastAPI:
    """Build the application.

    ``secret_key`` is resolved before this is called, by pitwatch.bootstrap,
    because Starlette's session middleware takes its key when it is added and
    the stored key does not exist until the database is up. Passing None is
    fine for tests, where no session outlives the process.
    """
    config = config or get_config()
    configure_logging(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with lifespan_pool(config) as pool:
            store = SettingsStore(pool)
            await store.load()
            await seed_from_environment(store, config)
            # There has to be a way in, so the first boot makes one. It cannot
            # go anywhere until its password is changed; see pitwatch.auth.
            await auth.ensure_default_admin(pool)

            live = LiveState()
            live_io = LiveIo()
            supervisor = Supervisor(pool, store, live, live_io)

            app.state.config = config
            app.state.pool = pool
            app.state.settings = store
            app.state.live = live
            app.state.live_io = live_io
            app.state.history = CurrentHistory()
            app.state.recent_runs = RecentRuns()
            app.state.signal_history = SignalHistory()
            app.state.supervisor = supervisor

            await supervisor.start()
            log.info("PitWatch %s is up on port %d", __version__, config.port)
            try:
                yield
            finally:
                log.info("Shutting down")
                await supervisor.stop()

    app = FastAPI(
        title="PitWatch",
        version=__version__,
        lifespan=lifespan,
        # The API is for this application's own front end, not a public
        # surface, so the generated docs are off by default.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Order matters and reads backwards: the last one added is the outermost,
    # so the session has to be added after the guard in order to run before it.
    app.add_middleware(SecurityHeaders)
    app.add_middleware(RequireSignIn)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key or config.secret_key or secrets.token_urlsafe(48),
        session_cookie="pitwatch_session",
        max_age=config.session_days * 24 * 60 * 60,
        # Marked Secure when a proxy is terminating TLS, so the cookie is never
        # sent in clear.
        https_only=config.secure_cookies,
        # Lax is what stops another site posting this application's forms with
        # your cookie attached. Strict would also break arriving from an
        # emailed invitation link, which is a normal thing to do.
        same_site="lax",
    )

    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")

    # Every template gets the version and the signed in user without each route
    # having to remember to pass them.
    templates.env.globals["version"] = __version__
    # Every template can render the token without each route remembering to
    # pass it, which is what stops one form quietly going without.
    templates.env.globals["csrf_token"] = _csrf_token
    # Shown on the policy pages, which are the sort of thing a carrier looks at
    # the date on.
    templates.env.globals["policy_updated"] = "26 August 2026"
    app.state.templates = templates

    users.register(app)
    pages.register(app)
    live_api.register(app)
    stream.register(app)

    # GET and HEAD both, explicitly. FastAPI does not add HEAD alongside GET the
    # way plain Starlette does, and `option httpchk HEAD /health` is a common
    # enough HAProxy line that answering it with a 405 would be a mean surprise.
    @app.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
    async def health() -> PlainTextResponse:
        """Is this process up and serving. For a load balancer or an uptime check.

        Plain text `ok` and a 200, and deliberately nothing else. It touches no
        database, because a proxy polls this every couple of seconds and there
        is no reason for that to become a query per poll, forever.

        It is not weaker than it looks. Uvicorn binds its socket only after the
        lifespan has finished starting, which includes connecting to Postgres
        and applying migrations, so a 200 from here means the application
        actually finished coming up. While it is still starting, or if it has
        died, the connection is refused, which every checker already reads as
        down.

        For "is it able to do its job right now", which is a different question
        and worth asking less often, use /healthz.
        """
        return PlainTextResponse("ok")

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        """Is this process able to serve requests, which means: can it reach
        the database. This is what the container health check uses.

        It answers from the database rather than from memory, because an app
        process that is running but cannot reach Postgres is not healthy in any
        sense that matters.
        """
        pool = getattr(request.app.state, "pool", None)
        if pool is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        try:
            await pool.fetchval("SELECT 1")
        except Exception as error:  # noqa: BLE001 -- any failure here is unhealthy
            log.warning("Health check could not reach the database: %s", error)
            return JSONResponse({"status": "unhealthy", "detail": str(error)}, status_code=503)
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/messaging-policy", include_in_schema=False)
    async def messaging_policy(request: Request) -> HTMLResponse:
        """Public on purpose. See pitwatch.middleware for why."""
        store: SettingsStore = request.app.state.settings
        return templates.TemplateResponse(
            request, "messaging_policy.html", {"site": store.site, "user": None}
        )

    @app.get("/privacy", include_in_schema=False)
    async def privacy(request: Request) -> HTMLResponse:
        store: SettingsStore = request.app.state.settings
        return templates.TemplateResponse(
            request, "privacy.html", {"site": store.site, "user": None}
        )

    @app.get("/contact", include_in_schema=False)
    async def contact(request: Request) -> HTMLResponse:
        """Public on purpose. See pitwatch.middleware for why."""
        store: SettingsStore = request.app.state.settings
        return templates.TemplateResponse(
            request, "contact.html", {"site": store.site, "user": None}
        )

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        """Two pages sharing an address.

        Signed out this is the public home page, which is the whole reason the
        root is not behind the login: a monitoring tool with no public face
        cannot be checked by a carrier reviewing a messaging registration, and
        cannot answer somebody who got a text about a pump and wants to know
        what sent it.

        Signed in it is the dashboard, or an invitation to set up.
        """
        store: SettingsStore = request.app.state.settings
        user = auth.current_user(request)
        if user is None:
            return templates.TemplateResponse(
                request, "home.html", {"site": store.site, "user": None}
            )

        # The middleware enforces this everywhere it guards, and this path is
        # now outside it. A default password is a password everybody knows.
        if user.must_change_password:
            return RedirectResponse("/change-password", status_code=303)

        if not await store.is_setup_complete():
            return templates.TemplateResponse(
                request, "index.html", {"site": store.site, "user": user, "setup_complete": False}
            )
        return templates.TemplateResponse(
            request, "dashboard.html", {"site": store.site, "user": user}
        )

    return app
