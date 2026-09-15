-- The water table the building sits in, measured rather than inferred.
--
-- The tide was a proxy for this. It was the best free number available and it
-- is genuinely correlated, but what actually fills a pit through a failed seal
-- is the height of the groundwater, and the tide is only one of the things
-- that moves it. Rain moves it, the season moves it, and so does whatever a
-- neighbor is doing to the ground next door.
--
-- It turns out the USGS keeps a monitoring well two hundred meters from the
-- reference installation, one of about six hundred they run across the city,
-- with a record going back two decades. Its Aug 1-19 mean was -0.60 ft in 2025
-- and -0.33 ft in 2026: a quarter foot higher year over year, measured by
-- somebody else, on equipment nobody here can misconfigure.
--
-- **This series is slow and it is late.** Daily values, published around a
-- month behind. That is useless for a dashboard lamp and it is exactly right
-- for the question it answers, which is not "what is happening now" but "is
-- this year's water table unusual". Everything downstream is written to know
-- the newest row is weeks old, and the card says the date rather than implying
-- the reading is current.

CREATE TABLE groundwater_reading (
    -- The day this describes. Daily values, so a date rather than a moment,
    -- stored as a timestamptz at UTC midnight to match every other series here.
    ts          timestamptz NOT NULL PRIMARY KEY,
    -- Feet relative to the well's datum, which for the reference well is
    -- NAVD88 and is usually slightly below zero: the water table sits a little
    -- under the reference surface. Stored exactly as published, because a
    -- conversion applied on the way in is a conversion nobody can check later.
    level       real NOT NULL,
    -- Which well said so. Kept per row rather than only in settings so that
    -- changing the well later does not silently restate old readings as having
    -- come from the new one.
    site_no     text NOT NULL,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX groundwater_reading_ts ON groundwater_reading (ts DESC);

-- Barometric pressure, which belongs with the weather because it arrives in
-- the same request.
--
-- A falling barometer lets groundwater discharge into a void more freely, so
-- on a pit fed by groundwater it should show up as more calls. In the first
-- five days of contact era data it correlates at about -0.25, which is the
-- right sign and too weak to act on. Worth collecting, not yet worth saying
-- anything about: the range over those days was only 19 hPa, and a barometer
-- needs a season to show what it does.
ALTER TABLE weather_hour ADD COLUMN pressure real;
