-- Who signed in, who tried and failed, and when.
--
-- There was a `last_login_at` column on `app_user` and nothing else. One
-- timestamp that overwrites itself is enough to answer "has this person ever
-- been here" and nothing at all beyond it. Failures went to the container's
-- standard output, which is a log that ends whenever the container is
-- recreated, and on 2026-09-16 that was three times in a morning.
--
-- The thing worth having is the failures. A successful sign in is somebody
-- getting on with their day. A run of failures against one account at four in
-- the morning is the only signal this application will ever get that somebody
-- is trying, and it was being written to a place nobody reads and nothing
-- keeps.
--
-- It matters more now than it did last week: this install has an outside
-- account on it, a manufacturer's engineer, and a button on the dashboard that
-- presses a contact on a live sewage panel.
--
-- No user_id. A failure usually names an account that does not exist, and a
-- foreign key would mean the rows worth keeping are exactly the ones that
-- cannot be stored. The name is recorded as typed.
CREATE TABLE sign_in_event (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    -- As typed, trimmed and cut. Somebody probing with a thousand character
    -- user name should not be able to decide how much disk this takes.
    username  text NOT NULL,
    -- 'ok', 'wrong' for a bad name or password, 'locked' for a request refused
    -- by the rate limit before any password was checked.
    outcome   text NOT NULL CHECK (outcome IN ('ok', 'wrong', 'locked')),
    -- Where from, as far as the proxy in front will say. Nullable because a
    -- request can arrive without one and a missing address is not a reason to
    -- lose the record that somebody tried.
    address   text
);

-- Read newest first, and almost always filtered to the failures.
CREATE INDEX sign_in_event_at ON sign_in_event (at DESC);
CREATE INDEX sign_in_event_failures ON sign_in_event (at DESC) WHERE outcome <> 'ok';
