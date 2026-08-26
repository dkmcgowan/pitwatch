"""Sending a text message.

Two providers, and they are not equivalent.

**Amazon SNS** publishes directly to a phone number. It is the one to use, and
the thing to know before choosing it is that sending to a US number is not a
sign-up-and-go affair: a new account sits in the SMS sandbox and can only reach
numbers it has verified, and reaching US numbers at all requires an origination
identity, meaning a registered 10DLC, a registered toll-free number, or a short
code. That is a regulatory requirement on US A2P messaging rather than anything
particular to AWS, and every other provider has the same one. The errors AWS
returns for it are unhelpfully worded, so they are translated below.

**A carrier email gateway** sends a short email to an address like
5551234567@vtext.com and lets the carrier turn it into a text. It costs nothing
and needs no registration. It is also unauthenticated, best effort, delivered
whenever the carrier feels like it, and being quietly withdrawn by most US
carriers. It is here because it is genuinely useful for a second, redundant path
to a phone, and it should not be anybody's only flood alarm.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import httpx2

from pitwatch.notify import email as email_sender
from pitwatch.notify.sigv4 import authorization_header
from pitwatch.schemas import SmsSettings, SmtpSettings

log = logging.getLogger(__name__)

TIMEOUT_S = 30
SNS_API_VERSION = "2010-03-31"

# E.164: a plus, then up to fifteen digits. AWS rejects anything else, with a
# message that does not say so.
E164 = re.compile(r"^\+[1-9]\d{7,14}$")


class SmsError(Exception):
    """Something the person configuring it can act on."""


def looks_like_a_number(value: str) -> bool:
    return bool(E164.match(value.strip()))


def normalize(value: str) -> str:
    """Tidy up how people actually type phone numbers.

    Nobody types +12125550142. They type (212) 555-0142, and a ten digit US
    number is unambiguous enough to fix silently rather than refuse.
    """
    cleaned = re.sub(r"[\s().-]", "", value.strip())
    if cleaned.startswith("+"):
        return cleaned
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return cleaned


def _sns_error(status: int, body: str) -> str:
    """Turn an SNS error body into something worth reading.

    AWS reports the two conditions that actually stop a new account from
    sending, the sandbox and the missing origination identity, in wording that
    gives no hint about what to do next.
    """
    lowered = body.lower()
    if "no origination identity" in lowered or "originationidentity" in lowered:
        return (
            "AWS has no origination identity for this destination. Sending to "
            "US numbers needs a registered 10DLC or toll-free number on the "
            "account; a plain AWS account cannot text a US phone without one."
        )
    if "sandbox" in lowered or "not verified" in lowered:
        return (
            "This AWS account is still in the SNS SMS sandbox, which can only "
            "reach verified numbers. Either verify this number in the SNS "
            "console under Text messaging, or request production access."
        )
    if status in (401, 403) or "signaturedoesnotmatch" in lowered:
        return (
            "AWS rejected the credentials. Check the access key and secret, and "
            "that the region matches the one the origination number is in."
        )
    if "invalidparameter" in lowered and "phonenumber" in lowered:
        return "AWS rejected the phone number. It has to be in +country format."
    return f"AWS returned {status}: {body[:400]}"


async def send_via_sns(settings: SmsSettings, to: str, message: str) -> None:
    if not settings.aws_access_key_id or not settings.aws_secret_access_key:
        raise SmsError("No AWS access key is configured")
    if not settings.aws_region:
        raise SmsError("No AWS region is configured")

    number = normalize(to)
    if not looks_like_a_number(number):
        raise SmsError(f"{to!r} does not look like a phone number. Use +1 and ten digits.")

    host = f"sns.{settings.aws_region}.amazonaws.com"
    fields = {
        "Action": "Publish",
        "Version": SNS_API_VERSION,
        "PhoneNumber": number,
        "Message": message,
    }

    # Transactional asks the carriers to prioritize delivery and costs a little
    # more. A pump alarm is the definition of transactional.
    attributes = [("AWS.SNS.SMS.SMSType", "Transactional")]
    if settings.origination_number:
        attributes.append(("AWS.MM.SMS.OriginationNumber", normalize(settings.origination_number)))
    if settings.sender_id:
        attributes.append(("AWS.SNS.SMS.SenderID", settings.sender_id))
    for index, (name, value) in enumerate(attributes, start=1):
        fields[f"MessageAttributes.entry.{index}.Name"] = name
        fields[f"MessageAttributes.entry.{index}.Value.DataType"] = "String"
        fields[f"MessageAttributes.entry.{index}.Value.StringValue"] = value

    body = urlencode(sorted(fields.items()), quote_via=quote).encode("utf-8")
    now = datetime.now(UTC)
    headers = {
        "host": host,
        "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
    }
    headers["authorization"] = authorization_header(
        access_key=settings.aws_access_key_id,
        secret_key=settings.aws_secret_access_key,
        region=settings.aws_region,
        service="sns",
        method="POST",
        path="/",
        query="",
        headers=headers,
        payload=body,
        now=now,
    )

    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(f"https://{host}/", content=body, headers=headers)
    except httpx2.HTTPError as error:
        raise SmsError(f"Could not reach {host}: {error}") from error

    if response.status_code >= 400:
        raise SmsError(_sns_error(response.status_code, response.text))

    log.info("Sent a text to %s through SNS", number)


async def send_via_gateway(
    settings: SmsSettings, smtp: SmtpSettings, to: str, message: str
) -> None:
    if not settings.gateway_domain:
        raise SmsError("No carrier gateway domain is configured, for example vtext.com")
    digits = re.sub(r"\D", "", to)
    if len(digits) < 10:
        raise SmsError(f"{to!r} does not have enough digits to be a phone number")

    address = f"{digits[-10:]}@{settings.gateway_domain.lstrip('@')}"
    try:
        # No subject. Carriers prepend it to the body, so it arrives as noise.
        await email_sender.send(smtp, address, "", message)
    except email_sender.EmailError as error:
        raise SmsError(f"The gateway send failed: {error}") from error


async def send(settings: SmsSettings, smtp: SmtpSettings, to: str, message: str) -> None:
    if settings.provider == "sns":
        await send_via_sns(settings, to, message)
    elif settings.provider == "email_gateway":
        await send_via_gateway(settings, smtp, to, message)
    else:  # pragma: no cover -- the model restricts this
        raise SmsError(f"Unknown SMS provider {settings.provider!r}")
