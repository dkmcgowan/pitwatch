"""Who gets told, and the record that they were.

Lifted out of the alert engine on 2026-09-09, unchanged, because a second thing
now needs it: the daily health check is news for the same people through the
same two channels, and it is not an alert. Copying thirty lines to send it would
have been two versions of "who wants to hear about this" drifting apart, and the
one that drifted would be the one nobody tested until the night it mattered.

**A failed send is recorded, not raised.** The row exists whether or not the
send worked, because "we tried to tell you and could not" is the thing somebody
needs to see afterwards, and a record written only on success is a record that
cannot show a failure.
"""

from __future__ import annotations

import logging

import asyncpg

from pitwatch.notify import email as email_sender
from pitwatch.notify import sms as sms_sender
from pitwatch.schemas import Severity
from pitwatch.settings import SettingsStore

log = logging.getLogger(__name__)

RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}

# Everything a person can be reached on. A caller passes fewer when the message
# is the wrong shape for one of them: several paragraphs of prose is four text
# messages and a surprise on somebody's phone bill.
EVERY_CHANNEL = ("email", "sms")


async def audience(pool: asyncpg.Pool, severity: Severity, admins_only: bool) -> list:
    """Everybody whose own level is at or below this one, and who is reachable."""
    try:
        return await pool.fetch(
            """
            SELECT name, email, phone, notify_email, notify_sms
            FROM app_user
            WHERE enabled
              AND (NOT $1::boolean OR is_admin)
              AND CASE min_severity
                      WHEN 'info' THEN 0 WHEN 'warning' THEN 1 ELSE 2
                  END <= $2::int
            """,
            admins_only,
            RANK[severity],
        )
    except (asyncpg.PostgresError, OSError) as error:
        log.error("Could not work out who to tell: %s", error)
        return []


async def tell(
    pool: asyncpg.Pool,
    store: SettingsStore,
    *,
    message: str,
    severity: Severity,
    event: str,
    admins_only: bool = False,
    alert_id: int | None = None,
    subject: str | None = None,
    channels: tuple[str, ...] = EVERY_CHANNEL,
) -> None:
    for person in await audience(pool, severity, admins_only):
        if "email" in channels and person["notify_email"] and person["email"]:
            await send(pool, store, alert_id, event, "email", person["email"], message, subject)
        if "sms" in channels and person["notify_sms"] and person["phone"]:
            await send(pool, store, alert_id, event, "sms", person["phone"], message, subject)


async def send(
    pool: asyncpg.Pool,
    store: SettingsStore,
    alert_id: int | None,
    event: str,
    channel: str,
    to: str,
    message: str,
    subject: str | None = None,
) -> None:
    """One message to one person, written down before it is attempted."""
    note_id = await pool.fetchval(
        """
        INSERT INTO notification (alert_id, event, channel, target, status, attempts)
        VALUES ($1, $2, $3, $4, 'pending', 1) RETURNING id
        """,
        alert_id,
        event,
        channel,
        to,
    )
    try:
        if channel == "email":
            site = store.site.where or "PitWatch"
            await email_sender.send(store.smtp, to, subject or f"PitWatch: {site}", message)
        else:
            await sms_sender.send(store.sms, to, message)
    except Exception as error:  # noqa: BLE001 -- one bad address must not stop the rest
        log.error("Could not send %s to %s: %s", channel, to, error)
        await pool.execute(
            "UPDATE notification SET status = 'failed', error = $2 WHERE id = $1",
            note_id,
            str(error)[:500],
        )
        return
    await pool.execute(
        "UPDATE notification SET status = 'sent', sent_at = now() WHERE id = $1", note_id
    )


__all__ = ["EVERY_CHANNEL", "RANK", "audience", "send", "tell"]
