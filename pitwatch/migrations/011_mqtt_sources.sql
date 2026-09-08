-- Two device sections become one broker and a list of sources.
--
-- The old settings named the hardware: a "shelly" row with an IP address and a
-- clamp mapping, and an "inputs" row with a broker and eight channels. Naming
-- the hardware is what forced a code change every time somebody wanted to use
-- different hardware, and it is also what tied the meter to a design where
-- PitWatch reached out to the device. That direction only works while the
-- application can see the panel's network.
--
-- This carries a running installation across without anybody retyping
-- anything. Everything that was configured stays configured, under a shape
-- that does not know what a Shelly is.
--
-- The clamp sources are filled in with topics and an ask payload that suit a
-- Shelly Gen2 or Gen3 meter, because that is what is on the pit this was
-- written against and a working default beats an empty box. Nothing in the
-- code knows that: they are four strings in a settings row, and anybody with
-- different hardware edits them. That is the whole point of the change.
--
-- Two things are deliberately not guessed at:
--
-- 1. The clamp channels. Which meter channel is pump 1 is a fact about which
--    clamp somebody put around which wire, and the readings already stored are
--    filed under those numbers. Changing them would leave last month's amps
--    describing the other pump.
-- 2. Whether the connection is on. It carries over the old inputs setting,
--    because the contacts were already arriving this way and they are the half
--    that decides whether a pump ran.

INSERT INTO setting (key, value, updated_at)
SELECT
    'mqtt',
    jsonb_build_object(
        -- The broker, verbatim from the inputs section. The contacts already
        -- came down this connection; it is now the only connection.
        'enabled',     coalesce(inputs.value -> 'enabled', 'false'::jsonb),
        'host',        coalesce(inputs.value ->> 'host', '127.0.0.1'),
        'port',        coalesce((inputs.value ->> 'port')::int, 1883),
        'username',    coalesce(inputs.value ->> 'username', ''),
        'password',    coalesce(inputs.value ->> 'password', ''),
        'encrypted',   coalesce(inputs.value -> 'encrypted', 'false'::jsonb),
        'client_id',   coalesce(inputs.value ->> 'client_id', 'pitwatch'),
        'debounce_ms', coalesce((inputs.value ->> 'debounce_ms')::int, 0),
        -- Which input carries what. Unchanged, because which input is the high
        -- float is a fact about the wiring rather than about the transport.
        'channels',    coalesce(inputs.value -> 'channels', '[]'::jsonb),
        'sources',     jsonb_build_array(
            -- The contacts, which need no invention: the topic, the profile
            -- and the shape are all already known and already working.
            jsonb_build_object(
                'name',    'Panel inputs',
                'role',    'contacts',
                'topic',   coalesce(inputs.value ->> 'topic', ''),
                'profile', 'contact_map',
                'path',    '',
                'expect_s', 0
            ),
            -- The module's own heartbeat, with the interval it was already
            -- being held to. This is the liveness signal now, because the
            -- broker's last will is not one: measured on 2026-09-07, an
            -- unplugged device kept its `online` topic true for the whole
            -- outage and published false a tenth of a second before it
            -- published true again.
            jsonb_build_object(
                'name',    'Panel module',
                'role',    'heartbeat',
                'topic',   coalesce(inputs.value ->> 'heartbeat_topic', ''),
                'profile', 'number',
                'path',    '',
                'expect_s', coalesce((inputs.value ->> 'heartbeat_s')::int, 0)
            ),
            -- The two clamps. There were no topics to carry over, because the
            -- meter was read over a websocket, so these are defaults rather
            -- than migrated values: the topics and the request a Shelly Gen2
            -- or Gen3 answers to. The request is the one thing a meter
            -- publishing on change cannot do for itself, and it is four
            -- strings in a settings row rather than anything the code knows.
            jsonb_build_object(
                'name',     'Pump 1 clamp',
                'role',     'clamp1',
                'topic',    'shellyemg3/status/em1:0',
                'profile',  'number',
                'path',     'current',
                'channel',  coalesce((shelly.value ->> 'pump1_channel')::int, 0),
                'expect_s', 45,
                'ask_topic',   'shellyemg3/rpc',
                'ask_payload',
                    '{"id":1,"src":"pitwatch","method":"EM1.GetStatus","params":{"id":0}}',
                'reply_topic', 'pitwatch/rpc',
                'reply_path',  'result.current',
                'ask_while_running', true,
                'ask_every_s', 1.0
            ),
            jsonb_build_object(
                'name',     'Pump 2 clamp',
                'role',     'clamp2',
                'topic',    'shellyemg3/status/em1:1',
                'profile',  'number',
                'path',     'current',
                'channel',  coalesce((shelly.value ->> 'pump2_channel')::int, 1),
                'expect_s', 45,
                'ask_topic',   'shellyemg3/rpc',
                'ask_payload',
                    '{"id":2,"src":"pitwatch","method":"EM1.GetStatus","params":{"id":1}}',
                'reply_topic', 'pitwatch/rpc',
                'reply_path',  'result.current',
                'ask_while_running', true,
                'ask_every_s', 1.0
            )
        )
    ),
    now()
FROM (SELECT value FROM setting WHERE key = 'inputs') AS inputs
FULL OUTER JOIN (SELECT value FROM setting WHERE key = 'shelly') AS shelly ON true
ON CONFLICT (key) DO NOTHING;

-- A fresh install has neither row, so the join above produces nothing and the
-- settings model's own defaults apply. Nothing to do here for that case.

-- And the old rows go, because the readers that read them are gone in the same
-- commit. Leaving settings behind for code that no longer exists is how a
-- settings table turns into an archaeology site.
DELETE FROM setting WHERE key IN ('shelly', 'inputs');

-- Sources report themselves by role, so device_status stops being a closed set
-- of device names. It was already widened once, to add the weather poller, and
-- widening it again per device is the wart that this whole change is about.
ALTER TABLE device_status DROP CONSTRAINT device_status_device_check;
ALTER TABLE device_status ADD CONSTRAINT device_status_device_check
    CHECK (device <> '' AND length(device) <= 40);

INSERT INTO device_status (device)
VALUES ('contacts'), ('heartbeat'), ('clamp1'), ('clamp2')
ON CONFLICT (device) DO NOTHING;

DELETE FROM device_status WHERE device IN ('shelly', 'inputs');
