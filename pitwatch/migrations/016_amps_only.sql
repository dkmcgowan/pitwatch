-- pitwatch: no-transaction
--
-- Continuous aggregates cannot be dropped inside a transaction block, so this
-- file is applied statement by statement. Every statement is written to be
-- safe to re-run.
--
-- Two rollups nothing read, and five columns nothing wrote.
--
-- The rollups first, because the columns were trapped behind them. em_1m and
-- em_1h were built so a chart covering a year would not read a year of one
-- second rows. Nothing ever asked them for anything: the history page buckets
-- em_sample directly, and the longest window it offers is 30 days. They were
-- refreshed on a schedule, retained on a policy, and queried by nobody.
--
-- Raw rows are cheap enough that the tier was solving a problem this
-- installation does not have. Half a day of a pit that runs for twelve seconds
-- at a time is four thousand rows and 672 kB before compression, and the
-- compression policy takes an order of magnitude off anything older than a
-- week. So the retention goes from 90 days to 400 instead, and the year over
-- year question the hourly rollup existed for is answered from the same table
-- as everything else.
--
-- Then the columns. voltage, act_power, aprt_power, pf and freq were one
-- meter's status frame, and they have been NULL on every row since a clamp
-- source became a topic and a path: a path reads one number, and the number a
-- pump monitor wants is the current. Voltage is the one worth saying why
-- about, because it looks like the useful one. A meter's own supply is not
-- necessarily the phase its clamps are around, so its voltage belongs to a
-- different circuit and everything derived from it, watts and power factor,
-- inherits that. Current does not care what the meter is plugged into.

DROP MATERIALIZED VIEW IF EXISTS em_1h;
DROP MATERIALIZED VIEW IF EXISTS em_1m;

SELECT remove_retention_policy('em_sample', if_exists => true);
SELECT add_retention_policy('em_sample', interval '400 days', if_not_exists => true);

ALTER TABLE em_sample DROP COLUMN IF EXISTS voltage;
ALTER TABLE em_sample DROP COLUMN IF EXISTS act_power;
ALTER TABLE em_sample DROP COLUMN IF EXISTS aprt_power;
ALTER TABLE em_sample DROP COLUMN IF EXISTS pf;
ALTER TABLE em_sample DROP COLUMN IF EXISTS freq;

-- A clamp setting that stopped being a setting. There is no switch for whether
-- to ask only while a pump runs, because that was a box with one sensible
-- value: on a pump monitor, a topic to ask on means ask while the pump turns.
-- An empty ask topic is how you say "do not ask".
UPDATE setting
SET value = jsonb_set(
        value,
        '{clamps}',
        (SELECT jsonb_agg(clamp - 'ask_while_running' ORDER BY clamp ->> 'pump')
         FROM jsonb_array_elements(value -> 'clamps') clamp)
    ),
    updated_at = now()
WHERE key = 'mqtt'
  AND value -> 'clamps' @> '[{"ask_while_running": true}]'::jsonb;
