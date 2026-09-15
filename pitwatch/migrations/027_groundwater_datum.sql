-- Which vertical datum a groundwater reading is measured against.
--
-- Added the same day the table was, after a real mix got stored. The well two
-- hundred meters from the reference installation publishes against both NGVD29
-- and NAVD88, and in New York those differ by about 1.09 ft: on 2025-03-14 it
-- read 0.08 against one and -1.01 against the other, the same water on the
-- same day.
--
-- The fetch asked for both and took whichever column came first, which over a
-- sixty day window was NAVD88 and over the full record was NGVD29. So the
-- backfill wrote a foot of phantom groundwater rise into a table whose whole
-- purpose is comparing this year against last.
--
-- The fetch now asks for one datum and falls back to the other only when a
-- well has no readings in the first. This column is the belt to that
-- suspenders: a datum recorded per row cannot be mixed silently, and a query
-- that compares across two of them can be made to refuse rather than to
-- average them into a number nobody can check.
--
-- Nullable, because rows written before this existed cannot be attributed
-- honestly. They are deleted rather than guessed at, but the column stays
-- nullable so that remains true of anything else that predates it.
ALTER TABLE groundwater_reading ADD COLUMN datum text;

-- Everything stored so far is the mix described above and cannot be untangled
-- row by row: the same day carries a different number depending on which
-- column the parser happened to land on. It is two months of a series that
-- refetches its whole history on startup, so throwing it away costs one
-- request and buys a record whose numbers mean one thing.
DELETE FROM groundwater_reading;
