-- Give every source that asks a question its own topic to hear the answer on.
--
-- Migration 011 gave both clamps the same reply topic and the same reply path,
-- which is ambiguous in a way that silently corrupts readings. An MQTT message
-- carries no sender and no sign of what it is answering: the topic is the whole
-- of its address. So a reply arriving on the shared topic matched both sources,
-- and pump 1's current would have been recorded as pump 2's as well.
--
-- Worse than a wrong number. It would have convinced the history page that
-- pump 2 had a clamp fitted, and drawn a line for a pump whose CT is not
-- installed, which is the one thing that page goes out of its way not to do.
--
-- It never fired: the ask only runs while a run contact is closed, and this
-- landed before the next call for water. Nothing recorded needs correcting.
--
-- The fix is a different src per source, because a device answers on whatever
-- src it was asked with. The model refuses the ambiguous pair now, so this
-- cannot come back by editing the settings page either.

UPDATE setting SET value = jsonb_set(
    value,
    '{sources}',
    (
        SELECT jsonb_agg(
            CASE
                WHEN source ->> 'role' = 'clamp1' AND source ->> 'ask_topic' <> '' THEN
                    source
                    || jsonb_build_object(
                        'ask_payload', replace(source ->> 'ask_payload',
                                               '"src":"pitwatch"', '"src":"pitwatch-c1"'),
                        'reply_topic', 'pitwatch-c1/rpc'
                    )
                WHEN source ->> 'role' = 'clamp2' AND source ->> 'ask_topic' <> '' THEN
                    source
                    || jsonb_build_object(
                        'ask_payload', replace(source ->> 'ask_payload',
                                               '"src":"pitwatch"', '"src":"pitwatch-c2"'),
                        'reply_topic', 'pitwatch-c2/rpc'
                    )
                ELSE source
            END
            ORDER BY ordinality
        )
        FROM jsonb_array_elements(value -> 'sources') WITH ORDINALITY AS entry(source, ordinality)
    )
)
WHERE key = 'mqtt' AND value -> 'sources' IS NOT NULL;
