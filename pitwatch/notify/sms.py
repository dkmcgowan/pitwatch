"""Sending a text message.

Two providers, and the choice between them is mostly about paperwork.

Either way, reaching a US number is not a sign-up-and-go affair. A registered
origination identity is required, meaning a 10DLC campaign, a toll-free number
or a short code, and the registration takes days and is reviewed by a human.
That is a rule about US A2P messaging rather than anything either vendor
invented, and there is no provider that does not have it.

**Twilio** is the default and the one being registered for here. Its API is a
form post, its errors say what is wrong in a sentence, and its console shows a
delivery receipt per message, which matters when the question is "did the alarm
actually arrive".

**Amazon SNS** publishes to a number the same way and is kept because it is
already wired and already works. Its errors are worded unhelpfully enough that
they are translated below.

A carrier email gateway was a third option and was removed. See SmsSettings for
why.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import httpx2

from pitwatch.notify.sigv4 import authorization_header
from pitwatch.schemas import SmsSettings

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


async def send_via_twilio(settings: SmsSettings, to: str, message: str) -> None:
    """Twilio's REST API, which is one form post and basic auth.

    No SDK. The whole call is an account SID, a secret, three form fields and a
    URL, and a dependency that has to be kept current for the rest of the
    project's life is a poor trade for the four lines it would save.

    Two ways to sign it, and the account SID is in the URL either way:

    - The account SID and its own auth token as the basic auth pair.
    - An API key SID and its secret as the pair, with the account SID still
      naming the account in the URL. This is what Twilio recommends, because a
      key can be revoked by itself.

    The account SID is therefore always required. Putting an API key SID in its
    place builds a URL for an account that does not exist, which Twilio answers
    with a 404 that says nothing useful, so that mistake is caught here.

    A messaging service is preferred over a bare number wherever one is set.
    That is what an A2P 10DLC registration is actually attached to, and sending
    from the number directly afterwards is unregistered traffic down a road
    that was registered.
    """
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        raise SmsError("No Twilio account SID and token are configured")
    if settings.twilio_account_sid.startswith("SK"):
        raise SmsError(
            "That is an API key SID in the account SID box. An API key does not "
            "replace the account SID: put the key SID in its own box, its secret "
            "in the token box, and the account SID that starts with AC here."
        )
    if not settings.twilio_messaging_service_sid and not settings.twilio_from:
        raise SmsError("Set a Twilio messaging service SID or a from number")

    number = normalize(to)
    if not looks_like_a_number(number):
        raise SmsError(f"{to!r} does not look like a phone number. Use +1 and ten digits.")

    fields = {"To": number, "Body": message}
    if settings.twilio_messaging_service_sid:
        fields["MessagingServiceSid"] = settings.twilio_messaging_service_sid
    else:
        fields["From"] = normalize(settings.twilio_from)

    url = (
        "https://api.twilio.com/2010-04-01/Accounts/"
        f"{quote(settings.twilio_account_sid)}/Messages.json"
    )
    # The key signs for the account rather than instead of it, so only the user
    # half of the pair changes.
    user = settings.twilio_key_sid or settings.twilio_account_sid
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
            response = await client.post(
                url,
                data=fields,
                auth=(user, settings.twilio_auth_token),
            )
    except httpx2.HTTPError as error:
        raise SmsError(f"Could not reach Twilio: {error}") from error

    if response.status_code >= 400:
        raise SmsError(_twilio_error(response.status_code, response.text))

    log.info("Sent a text to %s through Twilio", number)


def _twilio_error(status: int, body: str) -> str:
    """Twilio's own words where it gives them, which are unusually good.

    It answers JSON with a numeric code, a sentence and a documentation link.
    Reaching past that to say "SMS failed with 400" would be throwing away the
    one part of this that tells somebody what to fix.
    """
    message, code = None, None
    try:
        problem = json.loads(body)
    except ValueError:
        problem = None
    if isinstance(problem, dict):
        message = problem.get("message")
        code = problem.get("code")
    if message:
        return f"Twilio refused it ({code}): {message}" if code else f"Twilio refused it: {message}"
    return f"Twilio refused it with HTTP {status}: {body[:200]}"


async def send(settings: SmsSettings, to: str, message: str) -> None:
    if settings.provider == "twilio":
        await send_via_twilio(settings, to, message)
    elif settings.provider == "sns":
        await send_via_sns(settings, to, message)
    else:  # pragma: no cover -- the model restricts this
        raise SmsError(f"Unknown SMS provider {settings.provider!r}")
