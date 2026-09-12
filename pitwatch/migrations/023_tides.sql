-- The tide over the pit.
--
-- The same argument as the rain, one step further out. Rain is the water that
-- falls on the building; the tide is the water table the building sits in. On
-- the reference installation, a block from the Hudson on ground that used to be
-- the shoreline, it turned out to be the larger of the two: the pit's call rate
-- rose sevenfold within half an hour of the highest tide in nine days, and has
-- tracked high and low water ever since.
--
-- Nothing about that was visible from inside the building. The pumps were
-- healthy, the panel was sound, the meter accounted for a quarter of the water,
-- and the answer was a number published free by NOAA that nobody had thought to
-- put beside the pump record.
--
-- One row per six minutes, past and future in the same shape, same as
-- weather_hour and for the same reason: an hour that was a prediction yesterday
-- is a measurement today, so nothing marks a row as a forecast and whether a
-- row is ahead of now is decided by comparing it to now.

CREATE TABLE tide_reading (
    -- The moment this describes, in UTC. NOAA is asked in GMT so a row means
    -- the same thing after somebody moves the site to another timezone.
    ts          timestamptz NOT NULL PRIMARY KEY,
    -- Feet above MLLW, which is the datum the tide is published against and the
    -- one every chart and every tide table in the country agrees on. Stored in
    -- feet whatever the site chooses to read: one storage unit and one
    -- conversion at the edge.
    --
    -- Null ahead of now, because nothing has been observed yet.
    observed    real,
    -- What the harmonic prediction said, which exists on both sides of now.
    -- Keeping both is what makes the difference between them readable, and that
    -- difference is the storm surge: a foot of water the moon did not put there
    -- is the thing that floods a cellar.
    predicted   real,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tide_reading_ts ON tide_reading (ts DESC);

-- Reported like the weather poller, and for the same reason: a firewall that
-- blocks NOAA should be greppable rather than something to find in a container
-- log. No lamp on the dashboard, because a tide reading a quarter of an hour
-- stale is not a fault.
INSERT INTO device_status (device) VALUES ('tide') ON CONFLICT (device) DO NOTHING;
