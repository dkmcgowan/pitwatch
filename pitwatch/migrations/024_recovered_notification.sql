-- A notification about the panel being put right, which is a third thing.
--
-- The automatic recovery presses the panel's own button: silence when an
-- overload trips, then reset once the relay clears, which is the press that
-- actually returns the pump to the rotation. When that has worked there is a
-- sentence worth sending and no alert to hang it on, because by then nothing
-- is wrong. 'raised' and 'cleared' are the two halves of an alarm and
-- 'written' is the daily summary; none of them is this.
--
-- Found by a test on 2026-09-13, not in production. The send is wrapped in a
-- catch so a broken message cannot stop a recovery midway through pressing a
-- button, which meant this failed by writing a line in the log and delivering
-- nothing. Worth remembering the next time a new kind of message is added.

ALTER TABLE notification DROP CONSTRAINT IF EXISTS notification_event_check;

ALTER TABLE notification ADD CONSTRAINT notification_event_check
    CHECK (event IN ('raised', 'cleared', 'written', 'recovered'));
