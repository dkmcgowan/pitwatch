-- An answer that is still being written, and one that never arrived.
--
-- The page used to hold the POST open until the model replied. On a local model
-- thinking about a month of readings that is half a minute on a good day, and
-- behind Cloudflare it is a 524 at a hundred seconds: somebody else's error
-- page, with none of this application's wording on it, and the question
-- apparently lost. It was not lost -- the answer was in the database and on the
-- page when you came back -- but being told otherwise is being told a lie.
--
-- So the request returns straight away and the answer arrives underneath. Which
-- means a row has to be able to say "being written" and "did not arrive", and
-- those are the two columns here.

ALTER TABLE chat_message ADD COLUMN pending boolean NOT NULL DEFAULT false;

-- A failure is kept rather than thrown away, and shown in the thread where the
-- question is. The alternative is a page that silently forgets it was asked.
-- Never sent back to the model: see pitwatch.chat.paired.
ALTER TABLE chat_message ADD COLUMN failed boolean NOT NULL DEFAULT false;

-- Anything still marked pending when this runs was being written by a process
-- that is no longer running. There is nothing to wait for, so it is a failure,
-- and the sweep runs on every start for the same reason: see
-- pitwatch.chat.abandon.
UPDATE chat_message
SET pending = false, failed = true,
    content = 'This answer was interrupted by a restart. Ask again.'
WHERE pending;
