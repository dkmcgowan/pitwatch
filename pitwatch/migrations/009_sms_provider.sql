-- The carrier email gateway is gone as an SMS provider.
--
-- It sent a short email to an address like 5551234567@vtext.com and let the
-- carrier turn it into a text: free, no registration, and delivered whenever
-- the carrier felt like it, with no receipt and no way to tell a slow message
-- from one that was dropped. That is a fine trade for a reminder and not for a
-- flood alarm.
--
-- Anything still set to it moves to Twilio, which is what this installation is
-- registering with. The move is deliberately not silent about what it costs:
-- the Twilio credentials are not filled in by this, so an installation that
-- was on the gateway is now an installation whose SMS does not send until
-- somebody enters them. That is the honest state. Leaving it pointed at a
-- provider the code no longer has would fail the whole settings record
-- validation instead, and take the AWS keys stored beside it down with it.

UPDATE setting
SET value = jsonb_set(value, '{provider}', '"twilio"'),
    updated_at = now()
WHERE key = 'sms'
  AND value->>'provider' = 'email_gateway';

-- The gateway domain has nowhere to live now.
UPDATE setting
SET value = value - 'gateway_domain',
    updated_at = now()
WHERE key = 'sms'
  AND value ? 'gateway_domain';
