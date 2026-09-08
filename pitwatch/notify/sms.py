"""Sending a text message, through Twilio.

Reaching a US number is not a sign-up-and-go affair. A registered origination
identity is required, meaning a 10DLC campaign, a toll-free number or a short
code, and the registration takes days and is reviewed by a human. That is a
rule about US A2P messaging rather than anything a vendor invented.

Twilio's API is a form post, its errors say what is wrong in a sentence, and
its console shows a delivery receipt per message, which matters when the
question is "did the alarm actually arrive".

Two other providers have been in here and are gone. A carrier email gateway
was free and needed no registration and was delivered at the carrier's
convenience, which is the wrong shape for a flood alarm. Amazon SNS worked as
far as anybody could tell and that is the problem: the account it was written
against never came out of the SMS sandbox, so not one message was ever sent
through it. Code that has never run once is not a fallback, it is a guess with
a settings page, and it went along with the request signing it needed.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote

import httpx2

from pitwatch.schemas import SmsSettings

log = logging.getLogger(__name__)

TIMEOUT_S = 30

# E.164: a plus, then up to fifteen digits.
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
    await send_via_twilio(settings, to, message)
