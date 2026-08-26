-- People, replacing the split between "an account" and "a recipient".
--
-- Those were two tables describing the same person: one who could sign in, one
-- who got told when the pit flooded. Keeping them apart meant adding a
-- superintendent twice, and meant the alert list and the sign in list could
-- disagree about someone's phone number.
--
-- So there is one table. A row is a person. Whether they can sign in is
-- whether they have a password, and most people will not: the point of an
-- account for a building superintendent is that they get a text at two in the
-- morning, not that they log in.

DROP TABLE IF EXISTS recipient;

ALTER TABLE app_user
    ADD COLUMN name          text NOT NULL DEFAULT '',
    ADD COLUMN email         text,
    ADD COLUMN phone         text,
    -- What they want to be sent. Both off is a person who is recorded and
    -- deliberately not paged, which is a real thing to want.
    ADD COLUMN notify_email  boolean NOT NULL DEFAULT false,
    ADD COLUMN notify_sms    boolean NOT NULL DEFAULT false,
    ADD COLUMN min_severity  text NOT NULL DEFAULT 'warning'
                             CHECK (min_severity IN ('info', 'warning', 'critical')),
    ADD COLUMN is_admin      boolean NOT NULL DEFAULT false,
    -- Off means neither paged nor able to sign in. Better than deleting
    -- somebody who has left, because the alert history still points at them.
    ADD COLUMN enabled       boolean NOT NULL DEFAULT true,
    -- Set when an account is created with a default or a generated password.
    -- Signing in with one of those goes straight to a change password page and
    -- nowhere else.
    ADD COLUMN must_change_password boolean NOT NULL DEFAULT false;

-- A person who only ever receives alerts has no password at all, which is
-- different from having one nobody knows.
ALTER TABLE app_user ALTER COLUMN password_hash DROP NOT NULL;

-- One address per person, so that "who is this alert going to" has one answer.
CREATE UNIQUE INDEX app_user_email ON app_user (lower(email)) WHERE email IS NOT NULL;

-- Invitations and password resets.
--
-- Only the hash is stored. A token in this table is a bearer credential for
-- somebody's account, so a leaked database backup should not be a leaked set of
-- working invitation links.
CREATE TABLE password_token (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     integer NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    token_hash  text NOT NULL UNIQUE,
    purpose     text NOT NULL CHECK (purpose IN ('invite', 'reset')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);

CREATE INDEX password_token_user ON password_token (user_id) WHERE used_at IS NULL;
