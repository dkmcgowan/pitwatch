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
-- Three things are deliberately not guessed at:
--
-- 1. The meter's topics. It was read over a websocket, so there were no topics
--    to carry over, and inventing them would produce an installation that
--    looks configured and hears nothing. The clamp sources are written with
--    their roles, their channels and their ask payloads ready, and an empty
--    topic, which reads as "not configured yet" everywhere.
-- 2. The clamp channels. Which meter channel is pump 1 is a fact about which
--    clamp somebody put around which wire, and the readings already stored are
--    filed under those numbers. Changing them would leave last month's amps
--    describing the other pump.
-- 3. Whether MQTT is on. It carries over the old inputs setting, because the
--    contacts were already arriving this way and they are the half that
--    decides whether a pump ran.

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
            -- The two clamps, ready but not connected. A topic has to be typed
            -- in once, because there never was one: the meter was read over a
            -- websocket. Everything else is filled in, including the request
            -- that fetches a reading mid run, which is the one thing a meter
            -- publishing on change cannot do for itself.
            jsonb_build_object(
                'name',     'Pump 1 clamp',
                'role',     'clamp1',
                'topic',    '',
                'profile',  'number',
                'path',     'current',
                'channel',  coalesce((shelly.value ->> 'pump1_channel')::int, 0),
                'expect_s', 45,
                'ask_topic',   '',
                'ask_payload', '',
                'reply_path',  'result.current',
                'ask_while_running', true,
                'ask_every_s', 1.0
            ),
            jsonb_build_object(
                'name',     'Pump 2 clamp',
                'role',     'clamp2',
                'topic',    '',
                'profile',  'number',
                'path',     'current',
                'channel',  coalesce((shelly.value ->> 'pump2_channel')::int, 1),
                'expect_s', 45,
                'ask_topic',   '',
                'ask_payload', '',
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

-- The old rows stay for now. They are what this was built from, they cost a
-- few hundred bytes, and an operator who has to roll back to the previous
-- release should find their meter's address where they left it. They go when
-- the readers that read them go.

-- Sources report themselves by role, so device_status stops being a closed set
-- of device names. It was already widened once, to add the weather poller, and
-- widening it again per device is the wart that this whole change is about.
ALTER TABLE device_status DROP CONSTRAINT device_status_device_check;
ALTER TABLE device_status ADD CONSTRAINT device_status_device_check
    CHECK (device <> '' AND length(device) <= 40);

INSERT INTO device_status (device)
VALUES ('contacts'), ('heartbeat'), ('clamp1'), ('clamp2')
ON CONFLICT (device) DO NOTHING;
