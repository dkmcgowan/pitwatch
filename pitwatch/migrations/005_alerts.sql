-- Alerts and what was sent about them.
--
-- An alert is a condition that is true for a while, not an instant. It is
-- raised once, stays open, and clears when the condition goes away. The Magnus
-- panel's own contact is the opposite of this: a single pulse that says
-- something, somewhere, at some point. That difference is the product.

CREATE TABLE alert (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule             text NOT NULL,
    severity         text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    pump             smallint CHECK (pump IN (1, 2)),
    title            text NOT NULL,
    detail           text NOT NULL,
    -- Whatever the rule wants remembered: the amperage at the moment it fired,
    -- the float states, the run id. Alert emails are written from this, so an
    -- old alert still reads the way it did when it was sent.
    context          jsonb NOT NULL DEFAULT '{}'::jsonb,
    raised_at        timestamptz NOT NULL DEFAULT now(),
    cleared_at       timestamptz
    -- No acknowledgement columns. Silencing an alert by hand is a feature
    -- nobody has asked for, and a column waiting for one is a column that gets
    -- filled in wrongly by whoever assumes it already works.
);

CREATE INDEX alert_raised_at ON alert (raised_at DESC);

-- One open alert per rule per pump. This is the dedupe: a float that chatters
-- twenty times in a minute raises one alert, not twenty, because the second
-- insert conflicts and becomes an update instead.
CREATE UNIQUE INDEX alert_one_open_per_rule
    ON alert (rule, COALESCE(pump, 0)) WHERE cleared_at IS NULL;

CREATE TABLE notification (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alert_id    bigint REFERENCES alert (id) ON DELETE CASCADE,
    -- 'raised' or 'cleared'. The all clear matters as much as the alarm; being
    -- told the pit flooded and never told it drained is its own kind of bad.
    event       text NOT NULL CHECK (event IN ('raised', 'cleared')),
    channel     text NOT NULL CHECK (channel IN ('email', 'sms')),
    target      text NOT NULL,
    status      text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'failed', 'suppressed')),
    attempts    integer NOT NULL DEFAULT 0,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz
);

CREATE INDEX notification_pending ON notification (created_at) WHERE status = 'pending';
CREATE INDEX notification_alert ON notification (alert_id);
