"""Sending mail over SMTP.

Any SMTP server works. Amazon SES is the one this was built against, and SES
speaks ordinary SMTP, so there is nothing AWS specific here: its endpoint goes
in the host box and its SMTP credentials go in the user name and password
boxes. Note that SES SMTP credentials are **not** an IAM access key and secret;
they are generated separately in the SES console and only look similar. Pasting
an IAM key in is the single most common way to get an authentication failure
here, so the error text says so.
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


async def send(settings: SmtpSettings, to: str, subject: str, body: str) -> None:
    """Send one message, raising EmailError with something readable.

    aiosmtplib's own exceptions name SMTP status codes, which are precise and
    mean nothing to somebody who has just pasted the wrong kind of credential
    into a box.
    """
    if not settings.host:
        raise EmailError("No SMTP server is configured")
    if not settings.from_address:
        raise EmailError("No from address is configured. SES will reject mail without one.")
    if not looks_like_an_address(to):
        raise EmailError(f"{to!r} does not look like an email address")

    message = build(settings, to, subject, body)

    try:
        await aiosmtplib.send(
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
            "If this is Amazon SES, check you are using SES SMTP credentials "
            "rather than an IAM access key and secret; they are different "
            "things that look alike."
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

    log.info("Sent mail to %s via %s", to, settings.host)
