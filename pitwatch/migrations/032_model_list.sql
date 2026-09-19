-- The model stops being a box somebody types into.
--
-- It was one, and production spent a fortnight pointed at "qwen3.6-27b" while
-- the gateway offered "qwen3.8-27b-nvfp4". Every question failed. Nothing on
-- any page said so, because a typed name is only wrong at the far end of a
-- request. The list is read from the endpoint now, and picked from.
--
-- The thinking control and the model family profile go the same way, into the
-- gateway where they belong: set per model, by somebody who knows what that
-- model wants, rather than guessed here from a name. A gateway can offer the
-- same weights twice, thinking and not, which is a better answer than a
-- dropdown on this page ever was.
--
-- Whatever model was configured is carried across as a one entry list, marked
-- public so the chat keeps working through the deploy. Pressing Refresh on the
-- settings page replaces it with whatever the endpoint actually offers.

UPDATE setting
SET value = (value - 'model' - 'thinking' - 'profile') || jsonb_build_object(
        'models',
        CASE
            WHEN coalesce(trim(value #>> '{model}'), '') = '' THEN '[]'::jsonb
            ELSE jsonb_build_array(
                jsonb_build_object(
                    'id', value #>> '{model}',
                    'public', true,
                    -- The budget the whole application used before it was per
                    -- model. Right for the 160k window this was measured
                    -- against and worth revisiting per model on the page.
                    'budget', 100000
                )
            )
        END
    ),
    updated_at = now()
WHERE key = 'ai';
