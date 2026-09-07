-- Rain over the pit, hour by hour.
--
-- The whole point of this table is one question: was it raining when the pit
-- got busy, and is it about to. Everything else the weather API offers is
-- weather; this is the part that ends up in the pit.
--
-- One row per hour, past and future in the same shape. Nothing marks a row as
-- a forecast, because that is not a property of the row: an hour that was a
-- forecast yesterday is a measurement today, and a stored flag would have to
-- be swept and would still be wrong between sweeps. Whether a row is ahead of
-- now is decided by comparing it to now, which is always right.
--
-- A plain table rather than a hypertable. This is twenty four rows a day, it
-- is upserted rather than appended, and the same hour is rewritten several
-- times as the forecast for it firms up and then as it happens. Chunking and
-- compressing that would be work spent on nothing.

CREATE TABLE weather_hour (
    -- The hour this describes, on the hour, in UTC. The API is asked for UTC
    -- rather than for the site's zone so that a row means the same thing after
    -- somebody moves the site to another timezone, and so that nothing here
    -- has to reason about a clock going back.
    ts             timestamptz NOT NULL PRIMARY KEY,
    -- Millimeters that fell, or are expected to fall, in that hour. Stored in
    -- millimeters whatever the site chooses to read: one storage unit and one
    -- conversion at the edge, rather than a column whose meaning depends on a
    -- setting that can be changed later.
    precipitation  real,
    -- Percent chance of any precipitation. Meaningful ahead of now and merely
    -- historical behind it.
    probability    smallint,
    temperature    real,       -- Celsius, converted at the edge like the rain.
    -- The WMO code, which is what turns a number into "light rain". Kept
    -- because "0.4 mm" and "drizzle" answer different questions.
    code           smallint,
    -- When this row was last written. A forecast that stopped updating is a
    -- forecast worth distrusting, and this is how the page can tell.
    fetched_at     timestamptz NOT NULL DEFAULT now()
);

-- Every read is a window: the last day, the next day, the last month against
-- the calls in it.
CREATE INDEX weather_hour_ts ON weather_hour (ts DESC);

-- The poller reports itself like a device, so a firewall that blocks
-- Open-Meteo is greppable and legible rather than something to find in the
-- container log. It is deliberately not given a lamp on the dashboard: rain
-- that is a quarter of an hour stale is not a fault, and a red dot beside "the
-- Shelly is offline" would say it was. The rain card says how fresh it is
-- instead, which is the proportionate way to tell.
ALTER TABLE device_status DROP CONSTRAINT device_status_device_check;
ALTER TABLE device_status ADD CONSTRAINT device_status_device_check
    CHECK (device IN ('shelly', 'inputs', 'weather'));

INSERT INTO device_status (device) VALUES ('weather') ON CONFLICT (device) DO NOTHING;
