-- Sources with a profile become clamps, inputs and health checks.
--
-- The list of sources was one shape doing three jobs, and it carried a profile
-- to say which. That question turned out to have exactly one right answer per
-- job: a clamp reading is a number, a contact is on or off, and a health check
-- is a topic that ought to say something now and then. A setting with one right
-- answer is a setting that should not exist, so the profile is gone and each
-- kind has its own section.
--
-- **The contacts change shape on the panel, not just here.** They arrived as
-- eight states in one body, which is what one module happened to publish and
-- which cost a parser that had to guess how somebody had spelled eight keys.
-- One topic per contact is what a contact actually is. That means the module
-- has to be reconfigured to publish per contact, and until it is, the contacts
-- do not arrive.
--
-- That is a real gap in a flood monitor, so the topics below are written out
-- rather than left empty: set the module to publish each input to the matching
-- one and the panel comes back. They follow the topic the combined body used,
-- with the input number on the end.

UPDATE setting SET value = jsonb_build_object(
    'enabled',     value -> 'enabled',
    'host',        value ->> 'host',
    'port',        (value ->> 'port')::int,
    'username',    value ->> 'username',
    'password',    value ->> 'password',
    'encrypted',   value -> 'encrypted',
    'client_id',   value ->> 'client_id',
    'debounce_ms', (value ->> 'debounce_ms')::int,

    -- The two clamps, carried over whole. Their topics, paths, recording
    -- channels and the request that fetches a reading mid run all survive; the
    -- only thing dropped is the profile, which said "number" and could not
    -- have said anything else.
    'clamps', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'pump',              right(s ->> 'role', 1)::int,
            'topic',             coalesce(s ->> 'topic', ''),
            'path',              coalesce(s ->> 'path', ''),
            'channel',           coalesce((s ->> 'channel')::int, right(s ->> 'role', 1)::int - 1),
            'ask_topic',         coalesce(s ->> 'ask_topic', ''),
            'ask_payload',       coalesce(s ->> 'ask_payload', ''),
            'reply_topic',       coalesce(s ->> 'reply_topic', ''),
            'reply_path',        coalesce(s ->> 'reply_path', ''),
            'ask_while_running', coalesce(s -> 'ask_while_running', 'true'::jsonb),
            'ask_every_s',       coalesce((s ->> 'ask_every_s')::float, 1.0)
        ) ORDER BY s ->> 'role')
        FROM jsonb_array_elements(value -> 'sources') s
        WHERE s ->> 'role' LIKE 'clamp%'
    ), '[]'::jsonb),

    -- The inputs keep what they carry and which way round they read, and gain
    -- a topic each. Derived from the topic the combined body used so the
    -- module has an obvious thing to be pointed at.
    'inputs', coalesce((
        SELECT jsonb_agg(jsonb_build_object(
            'channel', (c ->> 'channel')::int,
            'role',    coalesce(c ->> 'role', ''),
            'topic',   CASE
                           WHEN coalesce(c ->> 'role', '') = '' THEN ''
                           ELSE coalesce(
                               (SELECT s ->> 'topic' FROM jsonb_array_elements(value -> 'sources') s
                                WHERE s ->> 'role' = 'contacts'),
                               'pitwatch/inputs'
                           ) || '/' || (c ->> 'channel')
                       END,
            'path',    '',
            'invert',  coalesce(c -> 'invert', 'false'::jsonb)
        ) ORDER BY (c ->> 'channel')::int)
        FROM jsonb_array_elements(value -> 'channels') c
    ), '[]'::jsonb),

    -- The heartbeat that was a source becomes the first health check, keeping
    -- the interval it was already held to.
    'health', jsonb_build_array(
        jsonb_build_object(
            'name',     'Panel module',
            'topic',    coalesce((SELECT s ->> 'topic' FROM jsonb_array_elements(value -> 'sources') s
                                  WHERE s ->> 'role' = 'heartbeat'), ''),
            'expect_s', coalesce((SELECT (s ->> 'expect_s')::int FROM jsonb_array_elements(value -> 'sources') s
                                  WHERE s ->> 'role' = 'heartbeat'), 0)
        ),
        -- And a second, empty, for the meter. Its own row because a clamp
        -- topic answers what the pump drew and this answers whether the thing
        -- that would have told us is still plugged in. They fail separately.
        jsonb_build_object('name', 'Meter', 'topic', '', 'expect_s', 0)
    )
)
WHERE key = 'mqtt' AND value -> 'sources' IS NOT NULL;

-- device_status now reports the clamps and the health checks. Eight contact
-- rows would be eight rows saying the same thing about one module, which is
-- what a health check is for.
INSERT INTO device_status (device) VALUES ('health0'), ('health1')
ON CONFLICT (device) DO NOTHING;

DELETE FROM device_status WHERE device IN ('contacts', 'heartbeat');
