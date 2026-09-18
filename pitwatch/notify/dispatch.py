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


async def audience(
    pool: asyncpg.Pool, site_id: int | None, severity: Severity, admins_only: bool
) -> list:
    """Everybody at this building whose own level is at or below this one.

    Scoped to the building, which is the difference between a text about a pump
    and a text about somebody else's pump. Membership is the test rather than
    the account existing: PitWatch's owner is reachable everywhere by being a
    member everywhere, and an administrator at one address is not woken at two
    in the morning by the address next door.

    Whether somebody counts as an administrator is also asked of the building.
    `admins_only` marks the messages that are about the equipment rather than
    about the water, and the person who can act on that is whoever administers
    *that* panel.
    """
    try:
        return await pool.fetch(
            """
            SELECT u.name, u.email, u.phone, u.notify_email, u.notify_sms
            FROM app_user u JOIN site_member m ON m.user_id = u.id
            WHERE m.site_id = $3
              AND u.enabled
              AND (NOT $1::boolean OR m.role IN ('admin', 'owner'))
              AND CASE u.min_severity
                      WHEN 'info' THEN 0 WHEN 'warning' THEN 1 ELSE 2
                  END <= $2::int
            """,
            admins_only,
            RANK[severity],
            site_id,
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
    for person in await audience(pool, store.site_id, severity, admins_only):
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
    # The building comes off the store rather than the alert, because a
    # notification that is not about an alert at all, the written summary and
    # the incident all clear, still belongs to somebody's building.
    note_id = await pool.fetchval(
        """
        INSERT INTO notification (site_id, alert_id, event, channel, target, status, attempts)
        VALUES ($1, $2, $3, $4, $5, 'pending', 1) RETURNING id
        """,
        store.site_id,
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
