"""Sending mail over SMTP.

Any SMTP server works. Amazon SES is the one this was built against, and SES
speaks ordinary SMTP, so there is nothing AWS specific here: its endpoint goes
in the host box and its SMTP credentials go in the user name and password
boxes.

**On the SES credentials, precisely.** The SMTP user name is an IAM access key
id. The SMTP password is derived from the matching secret access key by an
HMAC that includes the region, so it is not the secret key itself and a secret
key pasted straight in will be refused. The SES console does that derivation
when it offers to create SMTP credentials, which is why they look like a
separate thing, but deriving it yourself from an existing IAM key is perfectly
ordinary and works the same. What matters is that the password is the derived
one and that it was derived for the region you are sending through.
"""

from __future__ import annotations

import logging
import re
from email.message import EmailMessage

import aiosmtplib

from pitwatch.schemas import SmtpSettings

log = logging.getLogger(__name__)

TIMEOUT_S = 30

# Deliberately loose. The job is to catch a typo like a missing @ before a
# send fails, not to adjudicate what an address may contain.
ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailError(Exception):
    """Something went wrong that the person configuring it can act on."""


def looks_like_an_address(value: str) -> bool:
    return bool(ADDRESS.match(value.strip()))


def build(settings: SmtpSettings, to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = (
        f"{settings.from_name} <{settings.from_address}>"
        if settings.from_name
        else settings.from_address
    )
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return message


async def send(settings: SmtpSettings, to: str, subject: str, body: str) -> str:
    """Send one message. Returns what the server said, raising on failure.

    aiosmtplib's own exceptions name SMTP status codes, which are precise and
    mean nothing to somebody who has just pasted the wrong kind of credential
    into a box.

    **The server's reply is worth keeping.** A message the server accepted and
    then never delivered is the hardest kind to chase, and on Amazon SES the
    acceptance carries the message id: "250 Ok 010f0198...". That id is what
    turns "it never arrived" into something searchable, so it is logged and
    handed back to the test button rather than thrown away.
    """
    if not settings.host:
        raise EmailError("No SMTP server is configured")
    if not settings.from_address:
        raise EmailError("No from address is configured. SES will reject mail without one.")
    if not looks_like_an_address(to):
        raise EmailError(f"{to!r} does not look like an email address")

    message = build(settings, to, subject, body)

    try:
        errors, reply = await aiosmtplib.send(
            message,
            hostname=settings.host,
            port=settings.port,
            username=settings.username or None,
            password=settings.password or None,
            # 'tls' opens an already encrypted connection, usually on 465.
            # 'starttls' opens a plain one and upgrades it, usually on 587.
            use_tls=settings.security == "tls",
            start_tls=settings.security == "starttls",
            timeout=TIMEOUT_S,
        )
    except aiosmtplib.SMTPAuthenticationError as error:
        raise EmailError(
            f"The server rejected the user name and password ({error.code}). "
            "On Amazon SES the password is derived from an IAM secret access "
            "key rather than being the secret key itself, and the derivation "
            "includes the region, so a password made for one region will not "
            "work through another."
        ) from error
    except aiosmtplib.SMTPRecipientsRefused as error:
        raise EmailError(
            f"The server refused to deliver to {to}. If this is Amazon SES and "
            "the account is still in the sandbox, every recipient has to be "
            "verified first."
        ) from error
    except aiosmtplib.SMTPSenderRefused as error:
        raise EmailError(
            f"The server refused {settings.from_address} as a sender. On Amazon "
            "SES the from address has to be a verified identity."
        ) from error
    except aiosmtplib.SMTPConnectError as error:
        raise EmailError(
            f"Could not connect to {settings.host}:{settings.port}. Check the "
            "address and port, and that the security setting matches the port: "
            "587 is usually STARTTLS and 465 is usually TLS."
        ) from error
    except (aiosmtplib.SMTPException, OSError, TimeoutError) as error:
        raise EmailError(f"Sending failed: {error}") from error

    if errors:
        # Some recipients accepted and some refused. With one recipient this
        # cannot happen, but saying "sent" when the server disagreed is the
        # kind of lie that costs an afternoon.
        refused = ", ".join(f"{address} ({problem.code})" for address, problem in errors.items())
        raise EmailError(f"The server refused {refused}")

    log.info("Sent mail to %s via %s. The server said: %s", to, settings.host, reply)
    return reply.strip()
