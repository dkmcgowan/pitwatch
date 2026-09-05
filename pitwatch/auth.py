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

from pitwatch import csrf

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
    is_admin: bool
    enabled: bool
    must_change_password: bool
    has_password: bool
    # Derived from the stored hash, never the password. See
    # SESSION_FINGERPRINT_KEY.
    fingerprint: str

    @property
    def display_name(self) -> str:
        return self.name or self.username

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
            is_admin=row["is_admin"],
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
    is_admin, enabled, must_change_password, password_hash
"""


async def get_user(pool: asyncpg.Pool, user_id: int) -> User | None:
    row = await pool.fetchrow(f"SELECT {COLUMNS} FROM app_user WHERE id = $1", user_id)
    return User.from_row(row) if row else None


async def list_users(pool: asyncpg.Pool) -> list[User]:
    rows = await pool.fetch(
        f"SELECT {COLUMNS} FROM app_user ORDER BY is_admin DESC, lower(coalesce(name, username))"
    )
    return [User.from_row(row) for row in rows]


async def ensure_default_admin(pool: asyncpg.Pool) -> bool:
    """Create the first account if there is none. Returns True if it did.

    Shipping a known password is a real risk and the mitigation is
    must_change_password, which sends the first sign in to a change password
    page and refuses to let it go anywhere else.
    """
    if await pool.fetchval("SELECT EXISTS (SELECT 1 FROM app_user)"):
        return False

    await pool.execute(
        """
        INSERT INTO app_user (username, name, password_hash, is_admin, must_change_password)
        VALUES ($1, 'Administrator', $2, true, true)
        """,
        DEFAULT_USERNAME,
        hasher.hash(DEFAULT_PASSWORD),
    )
    log.warning(
        "Created the default %r account. Its password is the documented one and has to be "
        "changed at the first sign in.",
        DEFAULT_USERNAME,
    )
    return True


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


SignedIn = Annotated[User, Depends(require_user)]
IsAdmin = Annotated[User, Depends(require_admin)]
