-- Derived history: what the pumps actually did.
--
-- The raw samples say how many amps flowed at 3:04:11. These tables say "pump 2
-- ran for 41 seconds at 3:04, drawing 7.2 A steady with a 46 A inrush, and it
-- was the lag pump". That is the layer everything on the dashboard and every
-- alert rule reads.

-- A cycle is one call for water: the floats rose, one or both pumps ran, the
-- pit emptied. Grouping runs into cycles is what makes "both pumps ran at once"
-- answerable, and it is what the lead and lag inference walks.
CREATE TABLE pump_cycle (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at   timestamptz NOT NULL,
    ended_at     timestamptz,
    first_pump   smallint CHECK (first_pump IN (1, 2)),
    both_ran     boolean NOT NULL DEFAULT false,
    high_water   boolean NOT NULL DEFAULT false,
    lag_called   boolean NOT NULL DEFAULT false
);

CREATE INDEX pump_cycle_started_at ON pump_cycle (started_at DESC);

CREATE TABLE pump_run (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cycle_id    bigint REFERENCES pump_cycle (id) ON DELETE SET NULL,
    pump        smallint NOT NULL CHECK (pump IN (1, 2)),
    started_at  timestamptz NOT NULL,
    ended_at    timestamptz,
    duration_s  real,

    -- peak_current is the raw maximum, inrush included, because a motor whose
    -- starting surge is climbing month over month is a motor with a problem.
    peak_current    real,
    -- avg_current and steady_current both ignore the first seconds of the run.
    -- avg_current is a trimmed mean, steady_current the median. If they
    -- disagree, the run was not steady, which is itself worth seeing.
    avg_current     real,
    steady_current  real,
    min_current     real,
    samples         integer,

    -- There is deliberately no energy column, and no cost. Watt hours would
    -- have to come from the meter's power reading, and in this installation
    -- that reading is not about the motor: the meter's voltage reference is its
    -- own supply rather than a measured phase. Amps and duration are real
    -- measurements; anything multiplied by a voltage from somewhere else is a
    -- plausible looking number about the wrong circuit.
    --
    -- This is a monitor. What it is for is noticing that a pump is in trouble,
    -- not billing anybody for it.

    -- What we believed the pump's job was when it started. Recorded rather than
    -- computed on read, so later corrections to the inference do not silently
    -- rewrite what the dashboard said at the time.
    role        text NOT NULL DEFAULT 'unknown' CHECK (role IN ('lead', 'lag', 'unknown')),
    -- 'contact' when the Waveshare run signal started it, 'current' when only
    -- the clamp saw it. A run seen by one and not the other is a fault worth
    -- alerting on, so the source is kept.
    started_by  text NOT NULL CHECK (started_by IN ('contact', 'current', 'both')),
    ended_by    text CHECK (ended_by IN ('contact', 'current', 'both', 'timeout'))
);

CREATE INDEX pump_run_started_at ON pump_run (started_at DESC);
CREATE INDEX pump_run_pump_started_at ON pump_run (pump, started_at DESC);

-- Only one run per pump can be open at a time. Without this a missed stop edge
-- would quietly start a second overlapping run and every duration after it
-- would be wrong.
CREATE UNIQUE INDEX pump_run_one_open_per_pump ON pump_run (pump) WHERE ended_at IS NULL;

-- The alternation state, appended to rather than updated, so the history of
-- what we believed is recoverable. The Magnus panel never tells us which pump
-- is lead; this is inferred from which one started first last cycle.
CREATE TABLE lead_lag_state (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    effective_from  timestamptz NOT NULL,
    lead_pump       smallint NOT NULL CHECK (lead_pump IN (1, 2)),
    confidence      text NOT NULL CHECK (confidence IN ('high', 'low', 'unknown')),
    reason          text NOT NULL,
    cycle_id        bigint REFERENCES pump_cycle (id) ON DELETE SET NULL
);

CREATE INDEX lead_lag_state_effective_from ON lead_lag_state (effective_from DESC);
