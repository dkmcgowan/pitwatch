-- Three levels instead of a yes or no.
--
-- One boolean could say "this person may change anything", which by the time
-- the building's board, the super and a property manager all have accounts is
-- not a useful thing to be able to say. The split is one question: can this
-- silently stop the monitoring working, or press something physical?
--
--   viewer  the board, the pumps, the history, the summary. Gets the alerts.
--   admin   and the people, and the rules that decide what raises one.
--   owner   and the hardware: broker, topics, inputs, clamps, panel button.
--
-- Adding somebody cannot break anything. Changing an input's invert flag
-- turns an alarm off silently and nobody finds out until the night it
-- matters, which is why that one is kept behind the narrower door.
--
-- Everybody who was an administrator becomes an owner. Nobody loses access
-- they had this morning, and the person doing the migration is by definition
-- the person who set the thing up.

ALTER TABLE app_user ADD COLUMN role text NOT NULL DEFAULT 'viewer';

UPDATE app_user SET role = CASE WHEN is_admin THEN 'owner' ELSE 'viewer' END;

ALTER TABLE app_user ADD CONSTRAINT app_user_role_check
    CHECK (role IN ('viewer', 'admin', 'owner'));

-- is_admin stays a column no longer, because two ways to ask the same
-- question drift apart and the one that drifts is the one nobody tested.
ALTER TABLE app_user DROP COLUMN is_admin;
