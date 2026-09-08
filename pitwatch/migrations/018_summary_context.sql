-- What was written about the building when a summary was written.
--
-- The summary is the numbers plus a paragraph somebody wrote about the pit, and
-- the paragraph is the half that decides what the numbers mean. It was sent and
-- not kept, so a summary read a month later could be checked against the
-- readings it saw and not against the description it was given.
--
-- It is also what the page needs to know whether there is anything new to ask.
-- A week of readings and the same description gives the same answer for money,
-- so the button is dead until either the readings move on or somebody changes
-- what they said about the building. Comparing this against the current
-- description is how the page tells those apart.
--
-- Empty for every summary written before this, which reads correctly: nothing
-- is known about what those were told, so they never match the current
-- description and never hold the button down.

ALTER TABLE summary ADD COLUMN IF NOT EXISTS context text NOT NULL DEFAULT '';
