-- The input is the identity. What it is called is a label.
--
-- These tables recorded a signal name from a fixed list, which meant an input
-- could only be recorded as one of the things this application had heard of,
-- and anything else on the panel could not be watched at all. The channel was
-- always the real key: it is the terminal the wire lands on, and it is printed
-- on the module.
--
-- The column is kept rather than dropped, holding the label as it stood when
-- the reading was taken. Renaming an input from "High water" to "Top float"
-- should not rewrite what last month's history says it was called.

ALTER TABLE io_event RENAME COLUMN signal TO label;
ALTER TABLE io_state RENAME COLUMN signal TO label;

-- Every question worth asking of this history is about an input over time, so
-- that is what the index is on. Searching by name stopped making sense the
-- moment two inputs were allowed to share one.
DROP INDEX IF EXISTS io_event_signal_ts;
CREATE INDEX io_event_channel_ts ON io_event (channel, ts DESC);
