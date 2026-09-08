-- Amazon SNS is gone as an SMS provider.
--
-- It was written, wired, given a settings page and covered by tests, and not
-- one message was ever sent through it: the account it was written against
-- never came out of the SMS sandbox, so the whole path from the settings row
-- to the signed request was code nobody had watched work. A fallback that has
-- never run is not a fallback.
--
-- The same shape as the gateway removal before it. Anything still pointed at
-- SNS moves to Twilio and does not send until somebody fills the Twilio boxes
-- in, which is the honest state rather than a provider the code no longer has.
-- The difference is that this one takes the credentials with it, because the
-- keys stored here are AWS keys and there is nothing left that could use them.

UPDATE setting
SET value = jsonb_set(value, '{provider}', '"twilio"'),
    updated_at = now()
WHERE key = 'sms'
  AND value->>'provider' = 'sns';

UPDATE setting
SET value = value - 'aws_region' - 'aws_access_key_id' - 'aws_secret_access_key'
                  - 'origination_number' - 'sender_id',
    updated_at = now()
WHERE key = 'sms'
  AND (value ? 'aws_region' OR value ? 'aws_access_key_id'
       OR value ? 'aws_secret_access_key' OR value ? 'origination_number'
       OR value ? 'sender_id');
