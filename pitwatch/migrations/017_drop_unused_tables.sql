-- Two tables nothing has ever read.
--
-- `recipient` was the first answer to who gets told. 006 replaced it with
-- `app_user`, which holds the same three fields plus a way to sign in, and
-- nothing has selected from this since. It survived because dropping a table
-- is a decision and adding one is a habit.
--
-- `lead_lag_state` was going to hold an inference. The panel never says which
-- pump is lead, so the plan was to work it out from run history and append a
-- row each time the answer changed, with a confidence on it. Phase 6 settled
-- it differently and better: the first run in a cycle is the lead one, written
-- down on `pump_run` at the moment it starts, so there is nothing to infer and
-- no confidence to qualify. Not one row was ever written here.
--
-- Neither is referenced anywhere in the application, so there is nothing to
-- move and nothing to keep.

DROP TABLE IF EXISTS lead_lag_state;
DROP TABLE IF EXISTS recipient;
