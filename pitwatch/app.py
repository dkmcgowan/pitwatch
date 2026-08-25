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
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from pitwatch import __version__, auth
from pitwatch.api import live as live_api
from pitwatch.api import pages, stream
from pitwatch.config import Config, get_config
from pitwatch.db import lifespan_pool
from pitwatch.ingest.sink import LiveIo, LiveState
from pitwatch.ingest.supervisor import Supervisor
from pitwatch.settings import SettingsStore, seed_from_environment

log = logging.getLogger(__name__)

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

            live = LiveState()
            live_io = LiveIo()
            supervisor = Supervisor(pool, store, live, live_io)

            app.state.config = config
            app.state.pool = pool
            app.state.settings = store
            app.state.live = live
            app.state.live_io = live_io
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

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key or config.secret_key or secrets.token_urlsafe(48),
        session_cookie="pitwatch_session",
        https_only=False,
        same_site="lax",
    )

    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")

    # Every template gets the version and the signed in user without each route
    # having to remember to pass them.
    templates.env.globals["version"] = __version__
    app.state.templates = templates

    pages.register(app)
    live_api.register(app)
    stream.register(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        """Liveness and readiness in one, for the container health check.

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

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        """The dashboard, or an invitation to set up if nothing is configured.

        Readable without signing in, on the argument that a superintendent at a
        wall tablet should not have to type a password to see whether the pit
        is full. Changing anything still needs an account.
        """
        store: SettingsStore = request.app.state.settings
        if not await auth.any_user_exists(request.app.state.pool):
            return templates.TemplateResponse(
                request,
                "index.html",
                {"site": store.site, "user": None, "setup_complete": False},
            )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"site": store.site, "user": auth.current_user(request)},
        )

    return app
