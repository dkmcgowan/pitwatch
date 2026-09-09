-- The schedule grew two more answers, so its settings changed shape.
--
-- It was a checkbox: run one every day, or do not. That is one of three useful
-- cadences and it was the wrong default. A week of readings written every seven
-- days is the one somebody wants; today read every morning is a check before
-- the day starts, and thirty days read monthly is a trend. How often and how
-- much to read are separate questions and are separate settings now.
--
-- `daily` true becomes `daily`, which is what it meant. Anything with it off
-- lands on `off` and keeps the time it had. The window starts at a week for
-- everybody, because there was nothing in the old shape to carry over: it
-- always read a week.

UPDATE setting
SET value = (value - 'daily' - 'daily_at')
            || jsonb_build_object(
                   'schedule', CASE WHEN value->>'daily' = 'true' THEN 'daily' ELSE 'off' END,
                   'schedule_window', '7d',
                   'schedule_at', coalesce(value->>'daily_at', '07:00')
               ),
    updated_at = now()
WHERE key = 'summary'
  AND value ? 'daily';
