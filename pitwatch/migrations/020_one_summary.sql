-- One health summary, not a shelf of them.
--
-- Every check written was kept, on the reasoning that what it said in August is
-- the interesting question in September. In practice the way to answer a
-- question about August is to ask about August, which the window picker on the
-- page now does, and the stack of paragraphs underneath was a page and a table
-- earning nothing.
--
-- The readings are untouched. A summary is an opinion about what is in
-- em_sample and pump_run, and those are kept for four hundred days; deleting
-- the opinion loses nothing that cannot be asked again.
--
-- Written to survive being run on a database that already has one row, or none.

DELETE FROM summary
WHERE id NOT IN (SELECT id FROM summary ORDER BY created_at DESC LIMIT 1);
