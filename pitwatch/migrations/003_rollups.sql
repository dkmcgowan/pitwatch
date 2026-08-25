-- pitwatch: no-transaction
--
-- Continuous aggregates cannot be created inside a transaction block, so the
-- migration runner applies this file statement by statement without one. That
-- means a failure here can leave the migration half done; every statement is
-- written to be safe to re-run.
--
-- Two levels, minute and hour, with the hour built from the minute rather than
-- from the raw samples. Timescale refreshes them in the background, so a chart
-- covering a year does not read a year of one second rows.

CREATE MATERIALIZED VIEW IF NOT EXISTS em_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(interval '1 minute', ts) AS bucket,
    channel,
    avg(current)                         AS avg_current,
    max(current)                         AS max_current,
    min(current)                         AS min_current,
    avg(act_power)                       AS avg_act_power,
    max(act_power)                       AS max_act_power,
    avg(voltage)                         AS avg_voltage,
    avg(pf)                              AS avg_pf,
    count(*)                             AS samples
FROM em_sample
GROUP BY bucket, channel
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'em_1m',
    start_offset      => interval '3 hours',
    end_offset        => interval '1 minute',
    schedule_interval => interval '1 minute',
    if_not_exists     => true
);

CREATE MATERIALIZED VIEW IF NOT EXISTS em_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket(interval '1 hour', bucket)        AS bucket,
    channel,
    -- Weighting by sample count matters: a minute the device was offline for
    -- most of contributes fewer samples and should pull the hour less.
    sum(avg_current * samples) / nullif(sum(samples), 0)   AS avg_current,
    max(max_current)                                       AS max_current,
    min(min_current)                                       AS min_current,
    sum(avg_act_power * samples) / nullif(sum(samples), 0)  AS avg_act_power,
    max(max_act_power)                                     AS max_act_power,
    sum(samples)                                           AS samples
FROM em_1m
GROUP BY 1, 2
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'em_1h',
    start_offset      => interval '3 days',
    end_offset        => interval '1 hour',
    schedule_interval => interval '1 hour',
    if_not_exists     => true
);

-- Minute detail for a year, hourly forever. Neither is expensive; the point of
-- keeping the hourly rollup is being able to say "this pump has been drawing
-- half an amp more than it did last spring", which needs years, not days.
SELECT add_retention_policy('em_1m', interval '400 days', if_not_exists => true);
