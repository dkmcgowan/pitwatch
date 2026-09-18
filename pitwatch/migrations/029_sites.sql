-- More than one building.
--
-- Nothing here makes PitWatch multi-tenant today. There is one site, one
-- broker and one panel, and every page still shows that one. What this does is
-- put the column in before there are a hundred and fifty queries more of them,
-- because a query written without a site is a query that reads every
-- building's rows, and that mistake is silent: it passes every test written
-- against a single site and surfaces when somebody else's pump is on your
-- dashboard.
--
-- Adding it now costs a migration. Adding it in six months costs an audit.
--
-- **Row level security is not here and should be.** Scoping by hand relies on
-- every future query remembering, and the failure is invisible. Before a
-- second site ever holds real data, this wants RLS so the database refuses
-- rather than the developer remembering. Written down here because it is the
-- one thing that turns a missed WHERE from a leak into an empty result.

CREATE TABLE site (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- What somebody calls the building. The address and coordinates stay in
    -- the site settings, which is where they already are and where the pages
    -- read them from.
    name        text NOT NULL CHECK (name <> '' AND length(name) <= 120),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- The one that already exists. Named from the settings if they say anything,
-- because an installation that has been running for a fortnight should not
-- wake up calling itself "Site 1".
INSERT INTO site (name)
SELECT coalesce(nullif(trim(value #>> '{name}'), ''), 'This site')
FROM setting WHERE key = 'site';

INSERT INTO site (name)
SELECT 'This site'
WHERE NOT EXISTS (SELECT 1 FROM site);

-- Settings split in two, rather than one table with a nullable site.
--
-- A nullable site_id inside a unique key does not do what it looks like it
-- does: Postgres counts NULLs as distinct, so two application wide rows could
-- share a key and nothing would complain. Two tables also make the question
-- unambiguous at every call site, which is the point. SMTP, Twilio and the
-- model key belong to PitWatch. The panel, the pumps and the alert rules
-- belong to a building.
CREATE TABLE site_setting (
    site_id     integer NOT NULL REFERENCES site (id) ON DELETE CASCADE,
    key         text NOT NULL,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, key)
);

INSERT INTO site_setting (site_id, key, value, updated_at)
SELECT (SELECT min(id) FROM site), key, value, updated_at
FROM setting
WHERE key IN ('site', 'mqtt', 'pumps', 'alerts', 'panel_button', 'tide', 'groundwater', 'weather');

DELETE FROM setting
WHERE key IN ('site', 'mqtt', 'pumps', 'alerts', 'panel_button', 'tide', 'groundwater', 'weather');

-- The summary settings straddle the line, so they are split rather than
-- assigned. The key, the model and the base URL are one account serving every
-- building. The description, the schedule and whether to send it describe one
-- pit and one set of people.
INSERT INTO site_setting (site_id, key, value)
SELECT (SELECT min(id) FROM site), 'summary',
       coalesce(value, '{}'::jsonb) - 'api_key' - 'model' - 'base_url'
FROM setting WHERE key = 'summary';

UPDATE setting
SET value = jsonb_build_object(
        'api_key',  coalesce(value #>> '{api_key}', ''),
        'model',    coalesce(value #>> '{model}', 'gpt-4o-mini'),
        'base_url', coalesce(value #>> '{base_url}', 'https://api.openai.com/v1'))
WHERE key = 'summary';

UPDATE setting SET key = 'ai' WHERE key = 'summary';

-- Everything that is a reading, an event or a decision about one building.
--
-- NOT NULL with no default, after a backfill, because a default would let a
-- future insert quietly land in whichever site the default named. Better that
-- it fails loudly the first time somebody forgets.
ALTER TABLE io_event   ADD COLUMN site_id integer;
ALTER TABLE io_state   ADD COLUMN site_id integer;
ALTER TABLE em_sample  ADD COLUMN site_id integer;
ALTER TABLE pump_run   ADD COLUMN site_id integer;
ALTER TABLE pump_cycle ADD COLUMN site_id integer;
ALTER TABLE alert      ADD COLUMN site_id integer;
ALTER TABLE notification ADD COLUMN site_id integer;
ALTER TABLE device_status ADD COLUMN site_id integer;
ALTER TABLE summary    ADD COLUMN site_id integer;

UPDATE io_event      SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;
UPDATE io_state      SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;
UPDATE em_sample     SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;
UPDATE pump_run      SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;
UPDATE pump_cycle    SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;
UPDATE alert         SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;
UPDATE notification  SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;
UPDATE device_status SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;
UPDATE summary       SET site_id = (SELECT min(id) FROM site) WHERE site_id IS NULL;

ALTER TABLE io_event      ALTER COLUMN site_id SET NOT NULL;
ALTER TABLE io_state      ALTER COLUMN site_id SET NOT NULL;
ALTER TABLE em_sample     ALTER COLUMN site_id SET NOT NULL;
ALTER TABLE pump_run      ALTER COLUMN site_id SET NOT NULL;
ALTER TABLE pump_cycle    ALTER COLUMN site_id SET NOT NULL;
ALTER TABLE alert         ALTER COLUMN site_id SET NOT NULL;
ALTER TABLE notification  ALTER COLUMN site_id SET NOT NULL;
ALTER TABLE device_status ALTER COLUMN site_id SET NOT NULL;
ALTER TABLE summary       ALTER COLUMN site_id SET NOT NULL;

-- No foreign key on the two hypertables. Timescale carries one of these per
-- chunk and the constraint buys nothing a NOT NULL does not: nothing deletes a
-- site today, and when something does it will have to walk the chunks anyway.
ALTER TABLE pump_run     ADD CONSTRAINT pump_run_site      FOREIGN KEY (site_id) REFERENCES site (id);
ALTER TABLE pump_cycle   ADD CONSTRAINT pump_cycle_site    FOREIGN KEY (site_id) REFERENCES site (id);
ALTER TABLE alert        ADD CONSTRAINT alert_site         FOREIGN KEY (site_id) REFERENCES site (id);
ALTER TABLE notification ADD CONSTRAINT notification_site  FOREIGN KEY (site_id) REFERENCES site (id);
ALTER TABLE summary      ADD CONSTRAINT summary_site       FOREIGN KEY (site_id) REFERENCES site (id);

-- The keys that were unique across the whole installation and now are not.
--
-- io_state is one row per channel, and channel 3 means a different wire in
-- every building. device_status is one row per named device, and two buildings
-- will both call theirs "Inputs". The open alert index is the important one:
-- it is what stops the same alarm being raised twice, and left alone it would
-- stop one building's overload being raised because another building already
-- had one open.
ALTER TABLE io_state DROP CONSTRAINT io_state_pkey;
ALTER TABLE io_state ADD PRIMARY KEY (site_id, channel);

ALTER TABLE device_status DROP CONSTRAINT device_status_pkey;
ALTER TABLE device_status ADD PRIMARY KEY (site_id, device);

DROP INDEX alert_one_open_per_rule;
CREATE UNIQUE INDEX alert_one_open_per_rule
    ON alert (site_id, rule, COALESCE(pump, 0)) WHERE cleared_at IS NULL;

-- Read paths. Everything that was "lately, for this panel" is now "lately, for
-- this panel, in this building", and the leading column has to match.
CREATE INDEX io_event_site_ts    ON io_event (site_id, ts DESC);
CREATE INDEX em_sample_site_ts   ON em_sample (site_id, ts DESC);
CREATE INDEX pump_run_site       ON pump_run (site_id, started_at DESC);
CREATE INDEX pump_cycle_site     ON pump_cycle (site_id, started_at DESC);
CREATE INDEX alert_site_raised   ON alert (site_id, raised_at DESC);
CREATE INDEX notification_site   ON notification (site_id, created_at DESC);
CREATE INDEX summary_site        ON summary (site_id, created_at DESC);

-- Who can see which building.
--
-- A membership rather than a column on the account, because the shape that
-- matters later is one person holding different roles in different buildings:
-- the owner of their own and a viewer of the one next door. With one site it
-- is a row per user and nothing reads it yet.
CREATE TABLE site_member (
    site_id  integer NOT NULL REFERENCES site (id) ON DELETE CASCADE,
    user_id  integer NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    role     text NOT NULL CHECK (role IN ('viewer', 'admin', 'owner')),
    PRIMARY KEY (site_id, user_id)
);

INSERT INTO site_member (site_id, user_id, role)
SELECT (SELECT min(id) FROM site), id, role FROM app_user;
