"""Sign in, and the guard that decides who is allowed to change settings.

There is one account. This is a monitor for one pump room, not a multi tenant
application, and inventing roles for it would be inventing work. What the
account protects is the settings, the ability to silence an alarm, and the
history; the dashboard itself is readable without signing in, on the argument
that a building superintendent standing at a wall tablet should not have to
type a password to see whether the pit is full.

Passwords are hashed with Argon2id at the library defaults, which are chosen to
be slow on purpose.
"""

from __future__ import annotations

import logging
from typing import Annotated

import asyncpg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, status

log = logging.getLogger(__name__)

hasher = PasswordHasher()

SESSION_USER_KEY = "user"

MINIMUM_PASSWORD_LENGTH = 10


class PasswordTooShort(ValueError):
    def __init__(self) -> None:
        super().__init__(
            f"The password has to be at least {MINIMUM_PASSWORD_LENGTH} characters long"
        )


def hash_password(password: str) -> str:
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise PasswordTooShort
    return hasher.hash(password)


async def create_user(pool: asyncpg.Pool, username: str, password: str) -> int:
    return await pool.fetchval(
        """
        INSERT INTO app_user (username, password_hash)
        VALUES ($1, $2)
        ON CONFLICT (username) DO UPDATE SET password_hash = excluded.password_hash
        RETURNING id
        """,
        username.strip().lower(),
        hash_password(password),
    )


async def any_user_exists(pool: asyncpg.Pool) -> bool:
    return bool(await pool.fetchval("SELECT EXISTS (SELECT 1 FROM app_user)"))


async def authenticate(pool: asyncpg.Pool, username: str, password: str) -> str | None:
    """Return the username on success, None on any failure.

    The caller is not told which half was wrong, and a missing user still costs
    a hash verification, so the timing does not answer the question either.
    """
    row = await pool.fetchrow(
        "SELECT id, username, password_hash FROM app_user WHERE username = $1",
        username.strip().lower(),
    )
    stored = row["password_hash"] if row else _DUMMY_HASH
    try:
        hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        return None
    if row is None:
        return None

    if hasher.check_needs_rehash(stored):
        await pool.execute(
            "UPDATE app_user SET password_hash = $2 WHERE id = $1",
            row["id"],
            hasher.hash(password),
        )
    await pool.execute("UPDATE app_user SET last_login_at = now() WHERE id = $1", row["id"])
    return str(row["username"])


# Verified against when no such user exists, so that a wrong username and a
# wrong password take the same amount of time to reject.
_DUMMY_HASH = hasher.hash("a password that is nobody's password")


def current_user(request: Request) -> str | None:
    user = request.session.get(SESSION_USER_KEY)
    return str(user) if user else None


def sign_in(request: Request, username: str) -> None:
    request.session[SESSION_USER_KEY] = username


def sign_out(request: Request) -> None:
    request.session.pop(SESSION_USER_KEY, None)


def require_user(request: Request) -> str:
    """Dependency for anything that changes state.

    Raises 401 rather than redirecting, because every caller is either an API
    request from the front end or a form post, and both handle a status code
    better than they handle a login page arriving where JSON was expected. The
    page routes redirect on their own.
    """
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in first")
    return user


SignedIn = Annotated[str, Depends(require_user)]
