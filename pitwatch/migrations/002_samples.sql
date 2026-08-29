-- The two raw data streams: current from the CT clamps, contact state from the
-- panel module.
--
-- These are shaped very differently on purpose. The clamps produce a reading
-- roughly once a second forever, which is a time series and belongs in a
-- hypertable. The contacts change a handful of times a day, so storing a row
-- per poll would be almost entirely duplicate rows. Only edges get written.

-- One row per clamp reading. channel is the Shelly em1 instance id, 0 or 1,
-- not the pump number; which clamp is on which pump is a setting, and mapping
-- it here would bake a guess into the data.
CREATE TABLE em_sample (
    ts          timestamptz NOT NULL,
    channel     smallint NOT NULL,
    current     real,
    voltage     real,
    act_power   real,
    aprt_power  real,
    pf          real,
    freq        real
);

SELECT create_hypertable('em_sample', 'ts', chunk_time_interval => interval '1 day');

CREATE INDEX em_sample_channel_ts ON em_sample (channel, ts DESC);

-- Compressing after a week takes the raw samples down by an order of magnitude
-- and they are still queryable. Dropping them after 90 days is a default, not a
-- rule; the rollups in 003 keep the shape of the history for years.
ALTER TABLE em_sample SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'channel',
    timescaledb.compress_orderby = 'ts DESC'
);

SELECT add_compression_policy('em_sample', interval '7 days');
SELECT add_retention_policy('em_sample', interval '90 days');

-- One row per contact transition, already de-inverted: state is what the signal
-- means, raw is what the wire said. Keeping both means a normally closed
-- channel that was configured backwards can be spotted and corrected without
-- the history becoming a lie.
CREATE TABLE io_event (
    ts       timestamptz NOT NULL,
    channel  smallint NOT NULL CHECK (channel BETWEEN 1 AND 8),
    signal   text NOT NULL,
    state    boolean NOT NULL,
    raw      boolean NOT NULL
);

SELECT create_hypertable('io_event', 'ts', chunk_time_interval => interval '30 days');

CREATE INDEX io_event_signal_ts ON io_event (signal, ts DESC);

-- Current state, one row per channel, upserted by the reader. This exists so a
-- page load does not have to walk the event history backwards to answer "is the
-- high water float wet right now".
CREATE TABLE io_state (
    channel     smallint PRIMARY KEY CHECK (channel BETWEEN 1 AND 8),
    signal      text NOT NULL,
    state       boolean NOT NULL,
    raw         boolean NOT NULL,
    changed_at  timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
