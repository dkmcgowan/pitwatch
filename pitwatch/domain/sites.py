"""Which building a request is about, and who may look at it.

One installation watches one pit, and on that installation none of this is
visible: there is one site, everybody is a member of it, and no switcher is
drawn. It matters the day a second building exists, and it has to be in place
before that day rather than after it, because the alternative is a page that
quietly shows somebody else's pump.

**Two levels, not one.** PitWatch's owner is a person, not a building: they
hold the Twilio account, the mail server and the model key, which are one bill
for every installation. Everybody else holds a role *in a building* -- an
administrator at one address and a viewer at the one next door -- which is the
shape that matters as soon as two buildings share a deployment.

    app_user.role  = 'owner'  -> PitWatch's owner: every site, every switch
    site_member    = the role a person holds in one building

So `app_user.role` answers "is this the top level person", and `site_member`
answers "what may they do here". The effective role for a request is the second
one, with the first overriding it, and it is resolved once by the middleware and
carried on the `User` the rest of the request sees. Every `is_admin` check in
the application therefore keeps working unchanged and quietly becomes a question
about the building being looked at.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

import asyncpg

log = logging.getLogger(__name__)

# Where the chosen building is remembered. In the session rather than in the
# store, because the store is one object shared by every request and the choice
# belongs to one person looking at one browser tab.
SESSION_SITE_KEY = "site_id"

# What somebody may do in a building they are not a member of. Owners never
# reach this; for anybody else it is the honest answer, and it is `viewer`
# rather than `None` so a page renders read only instead of failing.
NO_MEMBERSHIP = "viewer"


@dataclass(frozen=True, slots=True)
class Site:
    id: int
    name: str


async def all_sites(pool: asyncpg.Pool) -> list[Site]:
    rows = await pool.fetch("SELECT id, name FROM site ORDER BY lower(name), id")
    return [Site(id=row["id"], name=row["name"]) for row in rows]


async def visible_to(pool: asyncpg.Pool, user) -> list[Site]:
    """Every building this person may look at.

    All of them for PitWatch's owner, and the ones they are a member of for
    everybody else. An owner who is not a member of anything still sees
    everything, which is the point of being the owner.
    """
    if user is None:
        return []
    if user.role == "owner":
        return await all_sites(pool)
    rows = await pool.fetch(
        """
        SELECT s.id, s.name
        FROM site s JOIN site_member m ON m.site_id = s.id
        WHERE m.user_id = $1
        ORDER BY lower(s.name), s.id
        """,
        user.id,
    )
    return [Site(id=row["id"], name=row["name"]) for row in rows]


async def role_in(pool: asyncpg.Pool, site_id: int, user_id: int) -> str | None:
    """The role this person holds in this building, or None if they hold none."""
    return await pool.fetchval(
        "SELECT role FROM site_member WHERE site_id = $1 AND user_id = $2",
        site_id,
        user_id,
    )


async def effective(pool: asyncpg.Pool, user, site_id: int | None):
    """The same user, carrying the role they hold in this building.

    Returned rather than mutated because `User` is frozen, and returned as a
    `User` rather than as a role so that every existing `user.is_admin` keeps
    reading the way it did. PitWatch's owner keeps `owner` everywhere: that is
    a fact about the person, not about the building.
    """
    if user is None or site_id is None or user.role == "owner":
        return user
    here = await role_in(pool, site_id, user.id)
    return replace(user, role=here or NO_MEMBERSHIP)


def pick(allowed: list[Site], chosen: int | None) -> int | None:
    """Which of these buildings this request is about.

    The one picked from the switcher, if it is still one this person may see --
    a membership can be taken away between one request and the next, and a
    stale number in a cookie must not outlive it.

    Otherwise the oldest building they are in, by id. By id and not by the
    order the list happens to be in: the list is sorted by name for the picker,
    and defaulting to the top of it meant that adding "14 Bank St" moved
    everybody's dashboard off 822 Greenwich, because 1 sorts before 8. The
    default has to be a building somebody chose, and the only one they ever
    chose is the one that was there first.
    """
    if chosen is not None and any(site.id == chosen for site in allowed):
        return chosen
    return min((site.id for site in allowed), default=None)


async def resolve(pool: asyncpg.Pool, user, chosen: int | None) -> int | None:
    """`pick` over everything this person may see. For callers with no list."""
    if user is None:
        return None
    try:
        return pick(await visible_to(pool, user), chosen)
    except (asyncpg.PostgresError, OSError) as error:
        # A page that cannot work out which building it is about cannot be
        # drawn safely, so this says "none" rather than guessing at one.
        log.error("Could not work out which sites %s may see: %s", user.username, error)
        return None


def store_for(request):
    """The settings this request is about, which is one building's.

    The middleware has already narrowed the application wide store to the site
    being looked at, so a route reads this rather than `app.state.settings`.
    The fallback is for the handful of paths that run before a site is
    settled -- the login page, the health check -- where the application wide
    store is the only one there is and nothing site scoped is read from it.
    """
    return getattr(request.state, "settings", None) or request.app.state.settings


def switcher(request) -> dict:
    """What the header needs to draw the building picker, if it draws one.

    The picker is PitWatch's owner's and nobody else's. They get it always, one
    building or ten, because it is how they know which building they are
    looking at and it should not appear and disappear as buildings come and go.

    Everybody else never sees it, whatever they are a member of. An empty
    `sites` is the signal the template uses to draw nothing at all. A site
    administrator or viewer looks after one basement and should not learn that
    PitWatch has a concept of a second one: for them the header must look
    exactly the way it looked before any of this existed.
    """
    known = list(getattr(request.state, "sites", None) or ())
    here = getattr(request.state, "site_id", None)
    user = getattr(request.state, "user", None)
    return {
        "sites": known if (user is not None and user.is_owner) else [],
        "site_id": here,
        # The name off the `site` row, which a building has from the moment it
        # is created. The settings have one too and it is the one the pages
        # print, but a building nobody has opened the settings for yet has an
        # empty one, and a heading reading "This building" above a picker
        # reading "14 Bank St" is two names for one place.
        "site_here": next((site for site in known if site.id == here), None),
    }


async def create(pool: asyncpg.Pool, name: str) -> int:
    """A new building, and nothing in it.

    No settings rows and no devices: an empty site reads as every default,
    which is what a building nobody has configured yet actually is. The person
    who made it is not made a member either -- an owner already sees every
    site, and anybody else has to be added deliberately.
    """
    return await pool.fetchval("INSERT INTO site (name) VALUES ($1) RETURNING id", name.strip())


__all__ = [
    "NO_MEMBERSHIP",
    "SESSION_SITE_KEY",
    "Site",
    "all_sites",
    "create",
    "effective",
    "pick",
    "resolve",
    "role_in",
    "store_for",
    "switcher",
    "visible_to",
]
