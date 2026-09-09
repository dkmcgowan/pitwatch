-- A notification that is not about an alert.
--
-- The daily health check is news for the same people through the same channel,
-- and it is not an alarm: nothing was raised and nothing will clear. It is
-- recorded here anyway, because the question "was I told?" is the same question
-- whatever the message was about, and a message that went out with no row is a
-- message nobody can account for afterwards.
--
-- `alert_id` was already nullable. `event` was not: it allowed 'raised' and
-- 'cleared', which are the two halves of an alarm and neither of them is this.

ALTER TABLE notification DROP CONSTRAINT IF EXISTS notification_event_check;

ALTER TABLE notification ADD CONSTRAINT notification_event_check
    CHECK (event IN ('raised', 'cleared', 'written'));
