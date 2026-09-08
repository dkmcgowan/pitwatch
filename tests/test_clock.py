"""Times written the way the building reads them."""

from __future__ import annotations

from datetime import UTC, datetime

from pitwatch import clock

# Mid afternoon in London, mid morning in New York, and the case that makes the
# bug obvious: the hour is different and so is the half of the day.
AFTERNOON = datetime(2026, 9, 8, 14, 47, tzinfo=UTC)


def test_a_time_is_written_on_the_sites_clock():
    """The bug this replaced: alerts were formatted with astimezone() and no
    argument, which is the *server's* zone. In a container that is UTC, so a
    text message reaching somebody in New York at ten to eleven in the morning
    said 14:47."""
    assert clock.at(AFTERNOON, "America/New_York") == "10:47 AM"
    assert clock.at(AFTERNOON, "Europe/London") == "3:47 PM"
    assert clock.at(AFTERNOON, "UTC") == "2:47 PM"


def test_the_hour_has_no_leading_zero():
    """Nine oh five in the morning reads as 9:05 AM, not 09:05 AM."""
    assert clock.at(datetime(2026, 9, 8, 13, 5, tzinfo=UTC), "America/New_York") == "9:05 AM"


def test_midnight_and_noon_are_not_the_same_time():
    """The one pair 12 hour time gets wrong if it is written by hand."""
    midnight = datetime(2026, 9, 8, 4, 0, tzinfo=UTC)
    noon = datetime(2026, 9, 8, 16, 0, tzinfo=UTC)

    assert clock.at(midnight, "America/New_York") == "12:00 AM"
    assert clock.at(noon, "America/New_York") == "12:00 PM"


def test_a_day_and_a_time_for_a_page_that_lists_them():
    assert clock.on_at(AFTERNOON, "America/New_York") == "8 Sep 10:47 AM"


def test_nothing_is_an_empty_string_rather_than_a_crash():
    assert clock.on_at(None, "America/New_York") == ""


def test_a_zone_this_machine_has_never_heard_of_is_left_alone():
    """A settings row can hold a zone that tzdata does not have. A wrong time
    is a smaller failure than an alert that cannot be written at all."""
    assert clock.at(AFTERNOON, "Mars/Olympus_Mons") == "2:47 PM"
    assert clock.at(AFTERNOON, "") == "2:47 PM"
