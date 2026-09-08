-- Name the health checks the way the dashboard names them, and drop the clamp
-- recording channel.
--
-- "Panel module" in settings and "Inputs" on the dashboard were the same thing
-- called two things, which is two things to somebody reading both pages.
--
-- The recording channel went from the model. It was a setting for exactly one
-- reason: readings already stored were filed under whatever numbers the meter
-- gave its clamps, and renumbering them would have left last month's amps
-- describing the other pump. The readings were wiped, so there is nothing left
-- to stay compatible with, and a box whose only correct value is the obvious
-- one is a box that can go. It is derived from the pump number now.

UPDATE setting SET value = jsonb_set(
    jsonb_set(
        value,
        '{clamps}',
        (SELECT jsonb_agg(clamp - 'channel' ORDER BY clamp ->> 'pump')
         FROM jsonb_array_elements(value -> 'clamps') clamp)
    ),
    '{health}',
    jsonb_build_array(
        jsonb_build_object(
            'name',     'Inputs',
            'topic',    coalesce(value -> 'health' -> 0 ->> 'topic', ''),
            'expect_s', coalesce((value -> 'health' -> 0 ->> 'expect_s')::int, 0)
        ),
        jsonb_build_object(
            'name',     'Meter',
            'topic',    coalesce(value -> 'health' -> 1 ->> 'topic', ''),
            'expect_s', coalesce((value -> 'health' -> 1 ->> 'expect_s')::int, 0)
        )
    )
)
WHERE key = 'mqtt' AND value -> 'clamps' IS NOT NULL;
