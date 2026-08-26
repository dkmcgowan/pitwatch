"""Notification logic that does not need a server to send to.

The transports themselves are covered by the test buttons in the browser, which
send real messages, because a send that does not actually deliver proves
nothing. What is here is the part that is wrong quietly: how a phone number
typed by a person becomes E.164, and whether an AWS refusal turns into something
worth reading.
"""

from __future__ import annotations

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


# -- turning AWS refusals into something actionable --------------------------


def test_a_missing_origination_identity_says_what_to_do():
    """The error that stops every new AWS account texting a US number.

    AWS words it as though you had forgotten a parameter. What it means is that
    you have to register a 10DLC or toll-free number, which takes days.
    """
    message = sms_sender._sns_error(
        400,
        "<Error><Code>InvalidParameter</Code><Message>No origination identity "
        "found for this account</Message></Error>",
    )

    assert "10DLC" in message
    assert "toll-free" in message


def test_the_sandbox_error_says_it_is_the_sandbox():
    message = sms_sender._sns_error(400, "Phone number is not verified in the SMS sandbox")

    assert "sandbox" in message.lower()
    assert "verify" in message.lower()


def test_a_signature_failure_points_at_the_credentials_and_the_region():
    message = sms_sender._sns_error(403, "<Code>SignatureDoesNotMatch</Code>")

    assert "credentials" in message.lower()
    assert "region" in message.lower()


def test_an_unrecognized_error_is_passed_through_rather_than_swallowed():
    message = sms_sender._sns_error(500, "Service Unavailable")

    assert "500" in message
    assert "Service Unavailable" in message


# -- refusing to send when it obviously cannot work --------------------------


async def test_sns_without_credentials_fails_before_making_a_request():
    with pytest.raises(sms_sender.SmsError, match="access key"):
        await sms_sender.send_via_sns(SmsSettings(aws_region="us-east-1"), "+12125550142", "hi")


async def test_sns_refuses_a_number_it_cannot_make_sense_of():
    settings = SmsSettings(
        aws_region="us-east-1", aws_access_key_id="AKIDEXAMPLE", aws_secret_access_key="secret"
    )

    with pytest.raises(sms_sender.SmsError, match="does not look like a phone number"):
        await sms_sender.send_via_sns(settings, "nonsense", "hi")


async def test_the_gateway_needs_a_carrier_domain():
    with pytest.raises(sms_sender.SmsError, match="gateway domain"):
        await sms_sender.send_via_gateway(
            SmsSettings(provider="email_gateway"), SmtpSettings(), "+12125550142", "hi"
        )


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
