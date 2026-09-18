"""Two buildings on one PitWatch, and the walls between them.

The companion to test_sites.py. That file asks whether a query remembers its
site; this one asks whether a *person* is held to theirs -- which is the half
that cannot be fixed by adding a WHERE clause, because it is about who is
asking rather than about what is stored.

Three claims, and every test here is one of them:

1. **The owner may cross.** PitWatch's owner switches buildings, and the
   dashboard, the history and the user list all follow. Settings that belong to
   PitWatch -- the mail server, the Twilio account, the model key -- do not
   follow, because there is one of each.
2. **Nobody else may.** An administrator at one address is a stranger at the
   next: they cannot switch to it, cannot be shown it by posting its id, and
   are not texted about its pump.
3. **On one building, none of this exists.** No switcher, no second list, and
   nothing on any page that would tell somebody watching one pit that PitWatch
   has a concept of a second one. This is the case every real installation is
   in today, and it is the one most easily broken by work on the other two.
"""

from __future__ import annotations

import pytest

from pitwatch import auth
from pitwatch.domain import sites

from .test_flow import NEW_PASSWORD, SETUP_FORM, sign_in_as_admin

pytestmark = pytest.mark.anyio


# -- fixtures ----------------------------------------------------------------


async def _a_second_building(pool, name: str = "14 Bank St") -> int:
    return await pool.fetchval("INSERT INTO site (name) VALUES ($1) RETURNING id", name)


async def _somebody(pool, username: str, site_id: int, role: str = "admin") -> int:
    """An account that is a member of exactly one building."""
    user_id = await pool.fetchval(
        """
        INSERT INTO app_user (username, name, email, notify_email, min_severity,
                              role, enabled, password_hash, must_change_password)
        VALUES ($1, $2, $3, true, 'info', 'viewer', true, $4, false)
        RETURNING id
        """,
        username,
        username.title(),
        f"{username}@example.com",
        auth.hash_password(NEW_PASSWORD),
    )
    await pool.execute(
        "INSERT INTO site_member (site_id, user_id, role) VALUES ($1, $2, $3)",
        site_id,
        user_id,
        role,
    )
    return user_id


def _sign_in(client, username: str):
    return client.post(
        "/login", data={"username": username, "password": NEW_PASSWORD}, follow_redirects=False
    )


# -- one building, which is every installation today -------------------------


def test_the_owner_sees_the_picker_even_with_one_building(client):
    """Always, one building or ten.

    It was drawn only when there was somewhere to go, which meant the control
    appeared the day a second building was added and the first one was the
    hardest to find. For the person who owns PitWatch the picker is how they
    know which building they are looking at, and a control that comes and goes
    is worse than one that sometimes has a single entry.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    page = client.get("/").text

    assert "site-switch" in page
    assert "Basement pit" in page


def test_the_picker_grows_as_buildings_are_added(client):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/sites/new", data={"new_site_name": "14 Bank St"})

    page = client.get("/").text

    assert "site-switch" in page
    assert "14 Bank St" in page
    assert "Basement pit" in page, "the one being looked at is still in the list"


# -- the owner crosses -------------------------------------------------------


def test_switching_changes_the_building_every_page_is_about(client, sql):
    """The whole point. One click, and the dashboard, the history and the user
    list are all about somewhere else."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/sites/new", data={"new_site_name": "14 Bank St"})
    second = sql("SELECT id FROM site WHERE name = '14 Bank St'", fetch=True)

    assert "Basement pit" in client.get("/settings").text

    client.post("/site/switch", data={"site_id": str(second)})

    page = client.get("/settings").text
    assert "14 Bank St" in page
    # The new building has no settings rows at all, so its name is the only
    # thing about it that is not a default. The broker from next door must not
    # have come with it.
    assert "192.168.1.51" not in page, "the first building's broker followed the switch"


def test_a_building_keeps_its_own_panel_and_shares_the_twilio_account(client, sql):
    """The line the settings page is split along.

    A second building gets its own broker, its own pumps and its own rules. It
    does not get its own Twilio account, because there is one account and one
    bill however many pits there are.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post(
        "/settings/sms",
        data={
            "sms_enabled": "on",
            "sms_twilio_account_sid": "AC-shared",
            "sms_twilio_auth_token": "shared-token",
            "sms_twilio_from": "+12125550100",
        },
    )
    client.post("/settings/sites/new", data={"new_site_name": "14 Bank St"})
    second = sql("SELECT id FROM site WHERE name = '14 Bank St'", fetch=True)
    client.post("/site/switch", data={"site_id": str(second)})

    page = client.get("/settings").text

    assert "AC-shared" in page, "the Twilio account belongs to PitWatch, not to a building"
    assert "192.168.1.51" not in page, "the broker belongs to a building, not to PitWatch"


def test_the_picker_carries_no_inline_handler(client):
    """The Content Security Policy is `script-src 'self'`.

    An `onchange` attribute renders perfectly, looks right in a screenshot and
    then does nothing at all: the browser refuses it and says so only in a
    console nobody has open. That is how it shipped the first time and was
    caught by driving a real browser rather than by any test here, so this is
    the test that was missing.
    """
    from pathlib import Path

    header = Path("pitwatch/templates/base.html").read_text(encoding="utf-8")
    switcher = header.split('class="site-switch"', 1)[1].split("</form>", 1)[0]

    assert "onchange" not in switcher, "an inline handler is dead on arrival under the CSP"
    assert "switcher.js" in header, "something has to wire the change event"
    # And the fallback for somebody with no JavaScript, which the script hides.
    assert "<button" in switcher


def test_the_two_halves_of_the_settings_page_say_which_is_which(client):
    """A page that mixed them is how somebody changes the Twilio account while
    meaning to change one building."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    page = client.get("/settings").text

    assert "Settings for this site" in page
    assert "Shared by every site" in page
    # The account moved out of the building's summary section and into its own.
    assert "/settings/ai" in page
    assert "AI account" in page


def test_an_owner_can_add_a_building_and_it_starts_empty(client, sql):
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    client.post("/settings/sites/new", data={"new_site_name": "14 Bank St"})

    assert sql("SELECT count(*) FROM site", fetch=True) == 2
    # Nothing in it. An empty site reads as every default, which is what a
    # building nobody has configured yet actually is.
    made = sql("SELECT id FROM site WHERE name = '14 Bank St'", fetch=True)
    assert sql("SELECT count(*) FROM site_setting WHERE site_id = $1", made, fetch=True) == 0


async def test_adding_a_building_does_not_move_anybody_off_the_one_they_watch(pool):
    """Caught by the test above before it could ship, and subtle enough to
    deserve its own line.

    The picker is sorted by name, and the default used to be the top of that
    list. So adding "14 Bank St" moved every owner's dashboard off 822
    Greenwich, because 1 sorts before 8 -- a building nobody asked to look at,
    arriving because somebody else typed a name. The default is the oldest
    building instead, which is the only one anybody ever chose.
    """
    first = await pool.fetchval("SELECT min(id) FROM site")
    await pool.execute("UPDATE site SET name = '822 Greenwich St' WHERE id = $1", first)
    await _a_second_building(pool, "14 Bank St")

    await pool.execute("DELETE FROM app_user")
    await auth.ensure_default_admin(pool)
    owner = await auth.get_user(pool, await pool.fetchval("SELECT id FROM app_user LIMIT 1"))

    seen = await sites.visible_to(pool, owner)
    assert [site.name for site in seen] == ["14 Bank St", "822 Greenwich St"], (
        "the picker lists by name, which is what made this possible"
    )
    assert sites.pick(seen, None) == first, "a new building moved the default"


def test_renaming_a_building_renames_it_in_the_picker_too(client, sql):
    """Two places hold the name and they have to agree. One renamed on its own
    form but not in the header is two buildings to anybody reading it."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)

    client.post(
        "/settings/site",
        data={**SETUP_FORM, "site_name": "822 Greenwich St", "site_timezone": "America/New_York"},
    )

    assert sql("SELECT name FROM site ORDER BY id LIMIT 1", fetch=True) == "822 Greenwich St"


# -- and nobody else does ----------------------------------------------------


def test_an_admin_at_one_address_cannot_switch_to_another(client, sql):
    """Posted, not clicked. The id arrives from a form, and a form is a thing
    anybody can send."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/sites/new", data={"new_site_name": "14 Bank St"})
    second = sql("SELECT id FROM site WHERE name = '14 Bank St'", fetch=True)
    first = sql("SELECT min(id) FROM site", fetch=True)

    sql(
        "INSERT INTO app_user (username, name, email, notify_email, min_severity, role,"
        " enabled, password_hash, must_change_password)"
        " VALUES ('tony', 'Tony', 'tony@example.com', true, 'info', 'viewer', true, $1, false)",
        auth.hash_password(NEW_PASSWORD),
    )
    who = sql("SELECT id FROM app_user WHERE username = 'tony'", fetch=True)
    sql(
        "INSERT INTO site_member (site_id, user_id, role) VALUES ($1, $2, 'admin')",
        first,
        who,
    )

    client.post("/logout")
    _sign_in(client, "tony")
    client.post("/site/switch", data={"site_id": str(second)})

    # Still the only building they are in. The refused switch is not an error
    # page: it is simply not honored, and no picker is drawn because from where
    # they stand there is still only one building.
    page = client.get("/")
    assert "14 Bank St" not in page.text
    assert "site-switch" not in page.text


def test_a_site_administrator_never_sees_the_picker_even_in_two_buildings(client, sql):
    """The other half of the rule, and the half worth a test of its own.

    Being in two buildings is not the same as being allowed to move between
    them from the header. The picker belongs to whoever owns PitWatch. An
    administrator who looks after two addresses still gets the header they had
    before any of this existed, and reaches the second one by signing in to
    whatever the owner has pointed them at.
    """
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/sites/new", data={"new_site_name": "14 Bank St"})
    first = sql("SELECT min(id) FROM site", fetch=True)
    second = sql("SELECT id FROM site WHERE name = '14 Bank St'", fetch=True)

    sql(
        "INSERT INTO app_user (username, name, email, notify_email, min_severity, role,"
        " enabled, password_hash, must_change_password)"
        " VALUES ('pat', 'Pat', 'pat@example.com', true, 'info', 'viewer', true, $1, false)",
        auth.hash_password(NEW_PASSWORD),
    )
    who = sql("SELECT id FROM app_user WHERE username = 'pat'", fetch=True)
    for site in (first, second):
        sql(
            "INSERT INTO site_member (site_id, user_id, role) VALUES ($1, $2, 'admin')",
            site,
            who,
        )

    client.post("/logout")
    _sign_in(client, "pat")
    page = client.get("/").text

    assert "site-switch" not in page, "a non owner was offered the building picker"
    assert "/site/switch" not in page
    # And they are still in both buildings, so the absence is the rule and not
    # a side effect of having nothing to pick.
    assert sql("SELECT count(*) FROM site_member WHERE user_id = $1", who, fetch=True) == 2


def test_a_switch_survives_the_next_page_load(client, sql):
    """Remembered in the session rather than in the URL, so every page follows
    without carrying a query string."""
    sign_in_as_admin(client)
    client.post("/setup", data=SETUP_FORM)
    client.post("/settings/sites/new", data={"new_site_name": "14 Bank St"})
    second = sql("SELECT id FROM site WHERE name = '14 Bank St'", fetch=True)

    client.post("/site/switch", data={"site_id": str(second)})

    for _ in range(2):
        page = client.get("/settings").text
        assert "14 Bank St" in page
        assert f'value="{second}" selected' in page.replace("\n", " ")


# -- who gets told -----------------------------------------------------------


async def test_an_alert_is_only_sent_to_the_people_at_that_building(pool):
    """The failure this prevents is somebody's phone going off at two in the
    morning about a pump in a building they have never been to."""
    from pitwatch.notify.dispatch import audience
    from pitwatch.schemas import Severity

    first = await pool.fetchval("SELECT min(id) FROM site")
    second = await _a_second_building(pool)
    await _somebody(pool, "here", first)
    await _somebody(pool, "there", second)

    at_first = await audience(pool, first, Severity.CRITICAL, admins_only=False)
    at_second = await audience(pool, second, Severity.CRITICAL, admins_only=False)

    assert [row["name"] for row in at_first] == ["Here"]
    assert [row["name"] for row in at_second] == ["There"]


async def test_being_an_administrator_is_a_question_about_a_building(pool):
    """`admins_only` marks the messages that are about the equipment. The person
    who can act on that is whoever administers *that* panel."""
    from pitwatch.notify.dispatch import audience
    from pitwatch.schemas import Severity

    first = await pool.fetchval("SELECT min(id) FROM site")
    second = await _a_second_building(pool)
    who = await _somebody(pool, "jordan", first, role="admin")
    await pool.execute(
        "INSERT INTO site_member (site_id, user_id, role) VALUES ($1, $2, 'viewer')", second, who
    )

    assert len(await audience(pool, first, Severity.WARNING, admins_only=True)) == 1
    assert await audience(pool, second, Severity.WARNING, admins_only=True) == []


async def test_the_first_account_on_a_new_install_can_be_reached(pool):
    """`ensure_default_admin` has to make a membership as well as an account.

    Being PitWatch's owner is enough to see a site and not enough to be told
    about one, so without the membership the only account on a fresh install
    would watch a pump it could never be texted about.
    """
    from pitwatch.notify.dispatch import audience
    from pitwatch.schemas import Severity

    await pool.execute("DELETE FROM app_user")
    assert await auth.ensure_default_admin(pool) is True

    site_id = await pool.fetchval("SELECT min(id) FROM site")
    assert len(await audience(pool, site_id, Severity.CRITICAL, admins_only=True)) == 1


# -- roles travel with the building ------------------------------------------


async def test_the_owner_owns_everywhere_and_everybody_else_does_not(pool):
    first = await pool.fetchval("SELECT min(id) FROM site")
    second = await _a_second_building(pool)

    await pool.execute("DELETE FROM app_user")
    await auth.ensure_default_admin(pool)
    boss = await pool.fetchrow(
        f"SELECT {auth.COLUMNS} FROM app_user WHERE username = $1", auth.DEFAULT_USERNAME
    )
    owner = auth.User.from_row(boss)

    admin_id = await _somebody(pool, "casey", first, role="admin")
    casey = await auth.get_user(pool, admin_id)

    # The owner is the owner in both, including one they are no member of.
    assert (await sites.effective(pool, owner, first)).is_owner
    assert (await sites.effective(pool, owner, second)).is_owner
    # And an administrator at one address is a stranger at the other.
    assert (await sites.effective(pool, casey, first)).is_admin
    assert not (await sites.effective(pool, casey, second)).is_admin


async def test_what_somebody_may_see_is_their_memberships(pool):
    first = await pool.fetchval("SELECT min(id) FROM site")
    second = await _a_second_building(pool)
    who = await _somebody(pool, "robin", second, role="viewer")
    robin = await auth.get_user(pool, who)

    assert [site.id for site in await sites.visible_to(pool, robin)] == [second]
    # And a choice they are not entitled to is not honored, which is what stops
    # a stale cookie outliving the membership that justified it.
    assert sites.pick(await sites.visible_to(pool, robin), first) == second


async def test_a_person_at_one_building_is_not_on_anothers_user_list(pool):
    """Both halves: the list, and the id typed into a URL."""
    first = await pool.fetchval("SELECT min(id) FROM site")
    second = await _a_second_building(pool)
    here = await _somebody(pool, "here", first)
    there = await _somebody(pool, "there", second)

    listed = {person.username for person in await auth.list_members(pool, first)}
    assert listed == {"here"}

    assert await auth.get_member(pool, first, here) is not None
    assert await auth.get_member(pool, first, there) is None, (
        "an id in the URL reached an account at another building"
    )
