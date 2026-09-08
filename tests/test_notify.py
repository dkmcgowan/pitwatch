"""Notification logic that does not need a server to send to.

The transports themselves are covered by the test buttons in the browser, which
send real messages, because a send that does not actually deliver proves
nothing. What is here is the part that is wrong quietly: how a phone number
typed by a person becomes E.164, and whether an AWS refusal turns into something
worth reading.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from pitwatch.notify import email as email_sender
from pitwatch.notify import sms as sms_sender
from pitwatch.schemas import SmsSettings, SmtpSettings

# -- phone numbers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("(212) 555-0142", "+12125550142"),
        ("212-555-0142", "+12125550142"),
        ("212.555.0142", "+12125550142"),
        ("2125550142", "+12125550142"),
        ("12125550142", "+12125550142"),
        ("+1 212 555 0142", "+12125550142"),
        ("+12125550142", "+12125550142"),
    ],
)
def test_the_ways_people_actually_type_a_number(typed, expected):
    """Nobody types E.164. AWS accepts nothing else, and says so unhelpfully."""
    assert sms_sender.normalize(typed) == expected


def test_a_number_that_is_already_international_is_left_alone():
    assert sms_sender.normalize("+442071838750") == "+442071838750"


def test_something_that_is_not_a_number_is_not_guessed_at():
    """Better to refuse than to invent a plausible number and text a stranger."""
    assert not sms_sender.looks_like_a_number(sms_sender.normalize("not a phone"))
    assert not sms_sender.looks_like_a_number(sms_sender.normalize("555-0142"))


def test_e164_validation():
    assert sms_sender.looks_like_a_number("+12125550142")
    assert not sms_sender.looks_like_a_number("12125550142"), "no plus"
    assert not sms_sender.looks_like_a_number("+0125550142"), "cannot start with zero"
    assert not sms_sender.looks_like_a_number("+1234567890123456"), "too long"


# -- Twilio ------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _CapturedClient:
    """Stands in for httpx2.AsyncClient and records the one post it is given.

    The delivery itself is proved by the test button in the browser, which
    sends a real message. What is worth testing without a network is the shape
    of the request, because getting it wrong fails at Twilio rather than here.
    """

    posted: ClassVar[dict] = {}

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url, data=None, auth=None):
        _CapturedClient.posted = {"url": url, "data": data, "auth": auth}
        return _FakeResponse(201, "{}")


@pytest.fixture
def twilio_post(monkeypatch):
    _CapturedClient.posted = {}
    monkeypatch.setattr(sms_sender.httpx2, "AsyncClient", _CapturedClient)
    return _CapturedClient


def _twilio(**extra) -> SmsSettings:
    fields = {
        "provider": "twilio",
        "twilio_account_sid": "AC0123456789",
        "twilio_auth_token": "a-token",
    }
    fields.update(extra)
    return SmsSettings(**fields)


async def test_twilio_without_credentials_fails_before_making_a_request():
    with pytest.raises(sms_sender.SmsError, match="account SID"):
        await sms_sender.send_via_twilio(SmsSettings(provider="twilio"), "+12125550142", "hi")


async def test_twilio_with_nothing_to_send_from_says_so():
    """A SID and a token are not enough. Something has to own the message."""
    with pytest.raises(sms_sender.SmsError, match="messaging service"):
        await sms_sender.send_via_twilio(_twilio(), "+12125550142", "hi")


async def test_twilio_refuses_a_number_it_cannot_make_sense_of():
    settings = _twilio(twilio_from="+18885550142")

    with pytest.raises(sms_sender.SmsError, match="does not look like a phone number"):
        await sms_sender.send_via_twilio(settings, "nonsense", "hi")


async def test_the_messaging_service_wins_over_a_bare_number(twilio_post):
    """The whole point of the A2P registration.

    An approved campaign hangs off the messaging service, so a message sent
    straight from the number is unregistered traffic even though both are
    configured and both would deliver today.
    """
    settings = _twilio(twilio_messaging_service_sid="MG9876", twilio_from="+18885550142")

    await sms_sender.send_via_twilio(settings, "(212) 555-0142", "the pit is full")

    sent = twilio_post.posted["data"]
    assert sent["MessagingServiceSid"] == "MG9876"
    assert "From" not in sent
    assert sent["To"] == "+12125550142", "typed loosely, sent as E.164"
    assert sent["Body"] == "the pit is full"


async def test_the_number_is_used_when_there_is_no_messaging_service(twilio_post):
    settings = _twilio(twilio_from="(888) 555-0142")

    await sms_sender.send_via_twilio(settings, "+12125550142", "hi")

    assert twilio_post.posted["data"]["From"] == "+18885550142"


async def test_the_account_sid_is_in_the_url_and_the_token_never_is(twilio_post):
    """A token on a URL ends up in logs at both ends. It belongs in the header
    that basic auth puts it in, and nowhere else."""
    await sms_sender.send_via_twilio(_twilio(twilio_from="+18885550142"), "+12125550142", "hi")

    assert twilio_post.posted["url"].endswith("/Accounts/AC0123456789/Messages.json")
    assert "a-token" not in twilio_post.posted["url"]
    assert twilio_post.posted["auth"] == ("AC0123456789", "a-token")


async def test_an_api_key_signs_for_the_account_rather_than_instead_of_it(twilio_post):
    """Twilio recommends an API key over the account auth token, and a key is a
    second thing alongside the account SID rather than a replacement for it.
    The key signs the request; the account SID still names the account the
    request is sent to."""
    settings = _twilio(
        twilio_key_sid="SK5555555555",
        twilio_auth_token="the-key-secret",
        twilio_from="+18885550142",
    )

    await sms_sender.send_via_twilio(settings, "+12125550142", "hi")

    assert twilio_post.posted["url"].endswith("/Accounts/AC0123456789/Messages.json")
    assert twilio_post.posted["auth"] == ("SK5555555555", "the-key-secret")


async def test_an_api_key_sid_in_the_account_box_is_caught_here(twilio_post):
    """The obvious way to read "use an API key instead" is to paste the key SID
    over the account SID, which builds a URL for an account that does not exist.
    Twilio answers that with a 404 and no useful words, so it is worth a
    sentence before the request is made."""
    settings = _twilio(twilio_account_sid="SK5555555555", twilio_from="+18885550142")

    with pytest.raises(sms_sender.SmsError, match="does not replace the account SID"):
        await sms_sender.send_via_twilio(settings, "+12125550142", "hi")

    assert twilio_post.posted == {}, "and nothing was sent"


async def test_a_refusal_from_twilio_is_raised_with_its_own_words(twilio_post):
    async def refuse(self, url, data=None, auth=None):
        return _FakeResponse(
            400,
            '{"code": 21610, "message": "Attempt to send to unsubscribed recipient"}',
        )

    twilio_post.post = refuse
    settings = _twilio(twilio_from="+18885550142")

    with pytest.raises(sms_sender.SmsError, match="unsubscribed recipient"):
        await sms_sender.send_via_twilio(settings, "+12125550142", "hi")


def test_an_unreadable_refusal_still_says_what_happened():
    """Twilio answers JSON until something in front of it does not, and a
    proxy's HTML page must not turn into a stack trace here."""
    message = sms_sender._twilio_error(502, "<html>Bad Gateway</html>")

    assert "502" in message
    assert "Bad Gateway" in message


async def test_email_refuses_without_a_server_or_a_from_address():
    with pytest.raises(email_sender.EmailError, match="No SMTP server"):
        await email_sender.send(SmtpSettings(), "you@example.com", "s", "b")

    with pytest.raises(email_sender.EmailError, match="from address"):
        await email_sender.send(SmtpSettings(host="smtp.example.com"), "you@example.com", "s", "b")


async def test_email_refuses_something_that_is_not_an_address():
    settings = SmtpSettings(host="smtp.example.com", from_address="pit@example.com")

    with pytest.raises(email_sender.EmailError, match="does not look like an email address"):
        await email_sender.send(settings, "not-an-address", "s", "b")


# -- the message itself ------------------------------------------------------


def test_the_from_header_carries_the_name_when_there_is_one():
    settings = SmtpSettings(
        host="smtp.example.com", from_address="pit@example.com", from_name="PitWatch"
    )

    message = email_sender.build(settings, "you@example.com", "Subject", "Body")

    assert message["From"] == "PitWatch <pit@example.com>"
    assert message["To"] == "you@example.com"
    assert message["Subject"] == "Subject"


def test_the_from_header_is_bare_when_there_is_no_name():
    settings = SmtpSettings(host="smtp.example.com", from_address="pit@example.com", from_name="")

    assert email_sender.build(settings, "you@example.com", "s", "b")["From"] == "pit@example.com"


def test_addresses_are_checked_loosely_rather_than_cleverly():
    assert email_sender.looks_like_an_address("someone@example.com")
    assert email_sender.looks_like_an_address("first.last+tag@sub.example.co.uk")
    assert not email_sender.looks_like_an_address("someone")
    assert not email_sender.looks_like_an_address("someone@localhost")
    assert not email_sender.looks_like_an_address("two @example.com")


async def test_the_servers_reply_comes_back(monkeypatch):
    """A message the server accepted and then never delivered is the hardest
    kind to chase. On Amazon SES the acceptance carries the message id, and
    that id is the only thing that makes it searchable afterwards.
    """
    import aiosmtplib

    from pitwatch.notify import email as email_sender

    async def accepted(message, **kwargs):
        return {}, "250 Ok 010f0198c2d1e4f5-abc"

    monkeypatch.setattr(aiosmtplib, "send", accepted)

    reply = await email_sender.send(
        SmtpSettings(host="smtp.example.com", from_address="alerts@example.com"),
        "you@example.com",
        "subject",
        "body",
    )

    assert reply == "250 Ok 010f0198c2d1e4f5-abc"


async def test_a_partly_refused_send_is_not_reported_as_sent(monkeypatch):
    """Saying sent when the server disagreed is the kind of lie that costs an
    afternoon."""
    import aiosmtplib

    from pitwatch.notify import email as email_sender

    class Refusal:
        code = 550

    async def partly(message, **kwargs):
        return {"you@example.com": Refusal()}, "250 Ok"

    monkeypatch.setattr(aiosmtplib, "send", partly)

    with pytest.raises(email_sender.EmailError, match="refused"):
        await email_sender.send(
            SmtpSettings(host="smtp.example.com", from_address="alerts@example.com"),
            "you@example.com",
            "subject",
            "body",
        )
