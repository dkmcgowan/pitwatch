-- The summary becomes a conversation.
--
-- The AI Health Summary wrote one paragraph, on a button or on a schedule, and
-- emailed it. Three things were wrong with that. It answered a question nobody
-- had asked yet; it could only speak in daily averages, because that is all it
-- was given; and the answer arrived with nowhere to put the follow up, which is
-- where every real question starts. "333 calls yesterday" is not the question.
-- "What happened on Friday at half past ten" is, and the old shape could not
-- hear it, let alone answer.
--
-- What replaces it is a chat, one thread per person per building, and the
-- window of data goes in with the first question rather than being boiled down
-- to a paragraph first.

-- One message. Rows are a transcript, so they are only ever appended and read
-- back in order.
CREATE TABLE chat_message (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    site_id     integer NOT NULL REFERENCES site (id) ON DELETE CASCADE,
    -- Whose thread. Per person rather than per building: a question is a
    -- half formed thought and nobody should have to ask theirs in public.
    -- ON DELETE CASCADE because a deleted account's questions go with it.
    user_id     integer NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    -- 'user' or 'assistant', the two the protocol has. The system prompt is
    -- built fresh on every request from the settings and the readings, so it is
    -- never stored: storing it would freeze a description somebody has since
    -- corrected, and freeze the numbers to whatever they were that afternoon.
    role        text NOT NULL CHECK (role IN ('user', 'assistant')),
    content     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Every read is "this person's thread in this building, oldest first".
CREATE INDEX chat_message_thread ON chat_message (site_id, user_id, created_at);

-- The written summary goes, and with it the last of the scheduling.
--
-- Dropped rather than kept for history: the table held at most one row per
-- building by design, every earlier one was deleted as each new one was
-- written, and production's was empty. There is nothing here to preserve.
DROP TABLE IF EXISTS summary;

-- The setting keeps its description and loses the schedule.
--
-- `description` is the half a model cannot work out from the readings, and it
-- is the whole reason this section survives at all. The rest described when to
-- write unasked and who to mail it to, and neither exists any more.
UPDATE site_setting
SET key = 'chat',
    value = value - 'schedule' - 'schedule_window' - 'schedule_at' - 'notify',
    updated_at = now()
WHERE key = 'summary';
