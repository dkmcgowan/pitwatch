"""Accounts, sign in, and the guard in front of everything.

One table holds people. Whether somebody can sign in is whether they have a
password, and most will not: the reason a building superintendent is in here is
to be texted at two in the morning, not to look at a web page. Admins configure
the system; everyone else can sign in, if they have set a password, and read the
dashboard.

Notes on the decisions, because they are the sort that get quietly undone:

* **Everything is behind sign in** except the login page, the health check, the
  static files, and the public terms and conditions. That last one is not an
  oversight: a carrier reviewing a toll-free number registration has to be able
  to read the opt-in terms without an account, and so does anybody deciding
  whether to give you their phone number.
* **The default password must be changed.** The first boot creates `admin` with
  a known password, because an appliance nobody can get into is useless. Signing
  in with it goes to a change password page and nowhere else, which is the only
  thing that makes shipping a known password defensible on a host that is
  reachable from the internet.
* **Passwords are Argon2id.** Slow on purpose.
* **Sign in is rate limited** per user name and per client address. This sits
  behind a proxy on a public name; an unthrottled login form there is an
  invitation.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status

from pitwatch import clock, csrf

log = logging.getLogger(__name__)

hasher = PasswordHasher()

SESSION_USER_KEY = "user_id"
SESSION_FRESH_KEY = "signed_in_at"
# A fingerprint of the password hash, checked on every request. Changing a
# password changes the hash, which changes this, which ends every other session
# for that account. Without it, a stolen cookie outlives the password change
# made because somebody thought it had been stolen.
SESSION_FINGERPRINT_KEY = "pw"

MINIMUM_PASSWORD_LENGTH = 10

# The account the first boot creates, so there is a way in at all.
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "pitwatch"

# How long an invitation or reset link is good for. Long enough to survive a
# night shift, short enough that a forwarded email is not a standing key.
TOKEN_LIFETIME = timedelta(days=3)

# Sign in throttling.
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW_S = 300
LOCKOUT_S = 300


class PasswordTooShort(ValueError):
    def __init__(self) -> None:
        super().__init__(
            f"The password has to be at least {MINIMUM_PASSWORD_LENGTH} characters long"
        )


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    name: str
    email: str | None
    phone: str | None
    notify_email: bool
    notify_sms: bool
    min_severity: str
    # viewer, admin or owner. Read through the two properties below rather
    # than compared to strings at the call sites, so what each level may do
    # is written down in one place.
    role: str
    enabled: bool
    must_change_password: bool
    has_password: bool
    # Derived from the stored hash, never the password. See
    # SESSION_FINGERPRINT_KEY.
    fingerprint: str

    @property
    def display_name(self) -> str:
        return self.name or self.username

    @property
    def is_admin(self) -> bool:
        """May manage people and the rules that decide what raises an alert."""
        return self.role in ("admin", "owner")

    @property
    def is_owner(self) -> bool:
        """May also change the hardware: broker, topics, inputs, clamps, the
        panel button. Everything on that list can stop the monitoring working
        without saying so."""
        return self.role == "owner"

    @classmethod
    def from_row(cls, row: asyncpg.Record) -> User:
        return cls(
            id=row["id"],
            username=row["username"],
            name=row["name"],
            email=row["email"],
            phone=row["phone"],
            notify_email=row["notify_email"],
            notify_sms=row["notify_sms"],
            min_severity=row["min_severity"],
            role=row["role"],
            enabled=row["enabled"],
            must_change_password=row["must_change_password"],
            has_password=row["password_hash"] is not None,
            fingerprint=fingerprint_of(row["password_hash"]),
        )


def fingerprint_of(password_hash: str | None) -> str:
    """A short, non reversible tag for a stored hash.

    The hash itself is already not the password, and this is a truncated digest
    of it, so a session cookie carries nothing useful even to somebody who can
    read it. All it has to do is change when the password does.
    """
    return hashlib.sha256((password_hash or "").encode()).hexdigest()[:16]


def hash_password(password: str) -> str:
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise PasswordTooShort
    return hasher.hash(password)


# Verified against when no such user exists, so a wrong user name and a wrong
# password cost the same and the timing does not answer the question.
_DUMMY_HASH = hasher.hash("a password that is nobody's password")


# -- throttling --------------------------------------------------------------
#
# In memory, so it resets when the container does. That is a real limit and an
# acceptable one: this is a single process appliance, and the alternative is a
# table written on every failed guess.

_attempts: dict[str, list[float]] = {}


def _record_failure(key: str) -> None:
    now = time.monotonic()
    recent = [at for at in _attempts.get(key, []) if now - at < ATTEMPT_WINDOW_S]
    recent.append(now)
    _attempts[key] = recent


def _clear_failures(key: str) -> None:
    _attempts.pop(key, None)


def seconds_locked_out(key: str) -> int:
    """How long this key has left to wait, or zero if it may try now."""
    now = time.monotonic()
    recent = [at for at in _attempts.get(key, []) if now - at < ATTEMPT_WINDOW_S]
    if len(recent) < MAX_ATTEMPTS:
        return 0
    return max(0, int(LOCKOUT_S - (now - recent[-1])))


def reset_throttling() -> None:
    """For tests, which would otherwise lock themselves out of the next one."""
    _attempts.clear()


# -- reading people ----------------------------------------------------------

COLUMNS = """
    id, username, name, email, phone, notify_email, notify_sms, min_severity,
    role, enabled, must_change_password, password_hash
"""


async def get_user(pool: asyncpg.Pool, user_id: int) -> User | None:
    row = await pool.fetchrow(f"SELECT {COLUMNS} FROM app_user WHERE id = $1", user_id)
    return User.from_row(row) if row else None


# One person as they exist in one building. The role that comes back is the one
# they hold here, except for PitWatch's owner, whose ownership is a fact about
# the person and follows them into every building. See pitwatch.domain.sites.
MEMBER_ROLE = """
    CASE WHEN u.role = 'owner' THEN 'owner' ELSE m.role END
"""


async def list_members(pool: asyncpg.Pool, site_id: int | None) -> list[User]:
    """Everybody at one building, most senior first.

    Not every account: a person who administers the building next door has no
    business on this list, and putting them on it is how somebody ends up
    disabling an account that was nothing to do with them. An installation with
    one site has one list and never notices the difference.
    """
    if site_id is None:
        return []
    rows = await pool.fetch(
        f"""
        SELECT u.id, u.username, u.name, u.email, u.phone, u.notify_email, u.notify_sms,
               u.min_severity, {MEMBER_ROLE} AS role, u.enabled, u.must_change_password,
               u.password_hash
        FROM app_user u JOIN site_member m ON m.user_id = u.id
        WHERE m.site_id = $1
        ORDER BY CASE WHEN u.role = 'owner' THEN 0 WHEN m.role = 'admin' THEN 1 ELSE 2 END,
                 lower(coalesce(u.name, u.username))
        """,
        site_id,
    )
    return [User.from_row(row) for row in rows]


async def get_member(pool: asyncpg.Pool, site_id: int | None, user_id: int) -> User | None:
    """One person as they exist in one building, or None if they are not in it.

    None is the answer that keeps an administrator at one address from editing
    somebody at another by typing their id into the URL.
    """
    if site_id is None:
        return None
    row = await pool.fetchrow(
        f"""
        SELECT u.id, u.username, u.name, u.email, u.phone, u.notify_email, u.notify_sms,
               u.min_severity, {MEMBER_ROLE} AS role, u.enabled, u.must_change_password,
               u.password_hash
        FROM app_user u JOIN site_member m ON m.user_id = u.id
        WHERE m.site_id = $1 AND u.id = $2
        """,
        site_id,
        user_id,
    )
    return User.from_row(row) if row else None


async def ensure_default_admin(pool: asyncpg.Pool) -> bool:
    """Create the first account if there is none. Returns True if it did.

    Shipping a known password is a real risk and the mitigation is
    must_change_password, which sends the first sign in to a change password
    page and refuses to let it go anywhere else.
    """
    if await pool.fetchval("SELECT EXISTS (SELECT 1 FROM app_user)"):
        return False

    # And a membership of every building there is, which on a fresh install is
    # the one the migration made. Being PitWatch's owner is enough to *see* a
    # site; it is not enough to be *told* about one, because an alert goes to
    # the people at that address and membership is the list. Without this the
    # first account on a new install would watch a pump it could never be
    # texted about, which is the one failure a monitor must not have.
    async with pool.acquire() as connection, connection.transaction():
        user_id = await connection.fetchval(
            """
            INSERT INTO app_user (username, name, password_hash, role, must_change_password)
            VALUES ($1, 'Administrator', $2, 'owner', true)
            RETURNING id
            """,
            DEFAULT_USERNAME,
            hasher.hash(DEFAULT_PASSWORD),
        )
        await connection.execute(
            "INSERT INTO site_member (site_id, user_id, role) SELECT id, $1, 'owner' FROM site",
            user_id,
        )
    log.warning(
        "Created the default %r account. Its password is the documented one and has to be "
        "changed at the first sign in.",
        DEFAULT_USERNAME,
    )
    return True


# How long a sign in record is kept.
#
# Long enough that somebody coming back from a fortnight away can still see
# what happened while they were gone, and not so long that this quietly becomes
# the largest table in a pump monitor.
SIGN_IN_KEPT = timedelta(days=90)

# Cut, because somebody probing with a thousand character user name should not
# get to decide how much disk this takes.
NAME_KEPT = 120


async def recent_sign_ins(pool: asyncpg.Pool, zone: str = "", limit: int = 25) -> list[dict]:
    """The last attempts, newest first, for the people page.

    Successes and failures together rather than a filter, because the useful
    reading is the shape: three failures and then a success is somebody who
    forgot their password, and thirty failures and no success is not.
    """
    try:
        rows = await pool.fetch(
            "SELECT at, username, outcome, address FROM sign_in_event ORDER BY at DESC LIMIT $1",
            limit,
        )
    except (asyncpg.PostgresError, OSError) as error:
        log.warning("Could not read the sign in log: %s", error)
        return []
    # Formatted here rather than in the template, on the building's own clock.
    # There is no date filter registered for Jinja and adding one for a single
    # table is more machinery than the table is worth.
    return [
        {
            "when": clock.on_at(row["at"], zone),
            "username": row["username"],
            "outcome": row["outcome"],
            "address": row["address"] or "",
        }
        for row in rows
    ]


async def record_sign_in(
    pool: asyncpg.Pool, username: str, outcome: str, address: str | None
) -> None:
    """Write down that somebody tried, whether or not they got in.

    The failures are the point. A success is somebody getting on with their
    day; a run of failures against one account at four in the morning is the
    only signal this application will ever get that somebody is trying, and
    until now it went to standard output, which ends when the container is
    recreated.

    Never raises. An application that will not let anybody sign in because it
    could not write down that they did is worse than one that forgets.
    """
    with contextlib.suppress(asyncpg.PostgresError, OSError):
        await pool.execute(
            "INSERT INTO sign_in_event (username, outcome, address) VALUES ($1, $2, $3)",
            (username or "")[:NAME_KEPT],
            outcome,
            address,
        )
        await pool.execute(
            "DELETE FROM sign_in_event WHERE at < now() - $1::interval", SIGN_IN_KEPT
        )


async def authenticate(pool: asyncpg.Pool, username: str, password: str) -> User | None:
    """Return the user on success, None on any failure.

    The caller is not told which half was wrong, and a missing user still costs
    a hash verification.
    """
    row = await pool.fetchrow(
        f"SELECT {COLUMNS} FROM app_user WHERE lower(username) = lower($1)",
        username.strip(),
    )
    stored = row["password_hash"] if row and row["password_hash"] else _DUMMY_HASH
    try:
        hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        return None
    if row is None or row["password_hash"] is None or not row["enabled"]:
        return None

    if hasher.check_needs_rehash(stored):
        await pool.execute(
            "UPDATE app_user SET password_hash = $2 WHERE id = $1", row["id"], hasher.hash(password)
        )
    await pool.execute("UPDATE app_user SET last_login_at = now() WHERE id = $1", row["id"])
    return User.from_row(row)


async def set_password(pool: asyncpg.Pool, user_id: int, password: str) -> None:
    await pool.execute(
        """
        UPDATE app_user
        SET password_hash = $2, must_change_password = false
        WHERE id = $1
        """,
        user_id,
        hash_password(password),
    )


# -- invitations and resets --------------------------------------------------


def _hash_token(token: str) -> str:
    """Tokens are stored hashed. A database backup is not a set of live links.

    SHA-256 rather than Argon2 on purpose: these are 32 bytes of randomness, so
    there is nothing to brute force, and this is checked on a page load.
    """
    return hashlib.sha256(token.encode()).hexdigest()


async def create_password_token(pool: asyncpg.Pool, user_id: int, purpose: str = "invite") -> str:
    """Issue a single use link token and return the plaintext, once."""
    token = secrets.token_urlsafe(32)
    async with pool.acquire() as connection, connection.transaction():
        # Any earlier unused token for this person stops working, so a resent
        # invitation does not leave the first one live.
        await connection.execute(
            "UPDATE password_token SET used_at = now() WHERE user_id = $1 AND used_at IS NULL",
            user_id,
        )
        await connection.execute(
            """
            INSERT INTO password_token (user_id, token_hash, purpose, expires_at)
            VALUES ($1, $2, $3, $4)
            """,
            user_id,
            _hash_token(token),
            purpose,
            datetime.now(UTC) + TOKEN_LIFETIME,
        )
    return token


async def redeem_password_token(pool: asyncpg.Pool, token: str) -> User | None:
    """Look a token up without spending it. Returns the user, or None."""
    row = await pool.fetchrow(
        f"""
        SELECT {COLUMNS} FROM app_user
        WHERE id = (
            SELECT user_id FROM password_token
            WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now()
        )
        """,
        _hash_token(token),
    )
    if row is None or not row["enabled"]:
        return None
    return User.from_row(row)


async def spend_password_token(pool: asyncpg.Pool, token: str) -> None:
    await pool.execute(
        "UPDATE password_token SET used_at = now() WHERE token_hash = $1", _hash_token(token)
    )


# -- the session -------------------------------------------------------------


def sign_in(request: Request, user: User) -> None:
    # A fresh CSRF token for the new identity, so a token handed out before
    # signing in cannot be used afterwards.
    csrf.rotate(request)
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_FRESH_KEY] = datetime.now(UTC).isoformat()
    request.session[SESSION_FINGERPRINT_KEY] = user.fingerprint


def sign_out(request: Request) -> None:
    request.session.clear()


def signed_in_user_id(request: Request) -> int | None:
    value = request.session.get(SESSION_USER_KEY)
    return int(value) if isinstance(value, int) else None


def current_user(request: Request) -> User | None:
    """The user the middleware already loaded for this request."""
    return getattr(request.state, "user", None)


def require_user(request: Request) -> User:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in first")
    return user


def require_admin(request: Request) -> User:
    user = require_user(request)
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an administrator can change this",
        )
    return user


def require_owner(request: Request) -> User:
    """The hardware, and anything that can stop the monitoring quietly.

    A separate door from the administrators because the two mistakes are not
    the same size. Adding the wrong person to the list is embarrassing and
    reversible in a minute. Ticking invert on an input turns an alarm off, and
    nothing looks any different until the night it was needed.
    """
    user = require_user(request)
    if not user.is_owner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the site owner can change this",
        )
    return user


SignedIn = Annotated[User, Depends(require_user)]
IsAdmin = Annotated[User, Depends(require_admin)]
IsOwner = Annotated[User, Depends(require_owner)]
