-- What the summary page has written.
--
-- Kept rather than replaced each time. A summary is a reading of the pit on a
-- particular morning, and the useful question a month later is what it said in
-- August, not what it says today. It is also a paid call to somebody else's
-- API, so throwing one away to make room for the next would be throwing away
-- something that cost money.
--
-- The numbers it was given are kept beside it. Without them a summary a month
-- old is an opinion with nothing to check it against.

CREATE TABLE summary (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    window_key  text        NOT NULL,
    model       text        NOT NULL,
    body        text        NOT NULL,
    facts       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- The account that pressed the button. Kept as a name rather than a
    -- reference: a summary should still say who asked for it after that
    -- account has been deleted.
    written_by  text        NOT NULL DEFAULT ''
);

CREATE INDEX summary_created_at ON summary (created_at DESC);
