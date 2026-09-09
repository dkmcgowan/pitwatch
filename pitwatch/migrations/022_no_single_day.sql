-- A day is not a window a summary reads, and not a cadence it runs on.
--
-- One day of a pit that calls every twenty minutes is a page of numbers with no
-- shape in it: nothing can have changed since yesterday that a week would not
-- show, and a paragraph a day about a pump that did what it did yesterday is a
-- paragraph nobody reads by Thursday. The history page keeps its day, because a
-- chart of today is a thing somebody watches while a pump is running.
--
-- Anything set to a day lands on the nearest thing that is still offered: a
-- daily schedule becomes weekly, a window of today becomes a week. Both are
-- what somebody choosing a day was reaching for once a day is not there.
--
-- Without this the stored value would fail to validate on the next boot, which
-- reads as the whole settings row being wrong rather than one field being from
-- a version that had one more choice in it.

UPDATE setting
SET value = jsonb_set(value, '{schedule}', '"weekly"'),
    updated_at = now()
WHERE key = 'summary' AND value->>'schedule' = 'daily';

UPDATE setting
SET value = jsonb_set(value, '{schedule_window}', '"7d"'),
    updated_at = now()
WHERE key = 'summary' AND value->>'schedule_window' = 'today';
