-- Core tables: settings, users, notification recipients, device health.
--
-- Nothing in this file is time series, so it is plain Postgres and would work
-- without the Timescale extension. The extension is created here because every
-- later migration depends on it.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Every configurable value the wizard and the settings pages write, one row per
-- key, JSON so a setting can grow a field without a migration. The environment
-- can seed these on first boot but never overrides them afterwards; see
-- pitwatch/settings.py.
CREATE TABLE setting (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
    id             integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username       text NOT NULL UNIQUE,
    password_hash  text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_login_at  timestamptz
);

-- Who gets told when something goes wrong. min_severity lets one person get
-- everything and another get only the calls worth waking up for.
CREATE TABLE recipient (
    id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL,
    email         text,
    phone         text,
    min_severity  text NOT NULL DEFAULT 'warning'
                  CHECK (min_severity IN ('info', 'warning', 'critical')),
    enabled       boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT recipient_needs_an_address CHECK (email IS NOT NULL OR phone IS NOT NULL)
);

-- One row per data source, so the dashboard can say "the Shelly stopped talking
-- to us at 3:12" instead of quietly showing a stale current forever. A pump
-- monitor that goes blind without saying so is worse than no pump monitor.
CREATE TABLE device_status (
    device       text PRIMARY KEY CHECK (device IN ('shelly', 'inputs')),
    online       boolean NOT NULL DEFAULT false,
    last_seen    timestamptz,
    last_error   text,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO device_status (device) VALUES ('shelly'), ('inputs');
