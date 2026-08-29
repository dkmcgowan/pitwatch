# PitWatch

Monitoring and real alerting for a duplex ejector pump panel.

Most pump controllers have one alarm contact and one thing to say with it:
something is wrong. Not which pump, not how wrong, not whether it has happened
before. PitWatch watches the same panel with a pair of current clamps and an
Ethernet I/O module and turns that one contact into a page that says *pump 2 has
been drawing 14.2 A for four minutes, the high water float is wet, and both
pumps are running*.

## What it does

Working now:

- Reads running current from both motors continuously, over a websocket the
  Shelly pushes to, and keeps the history.
- Reads the float switches, the run contacts and the overload contacts from the
  panel's own dry contacts.
- A live dashboard laid out like the panel: both pumps side by side with their
  current, the floats in the order they sit in the pit, and whether each device
  is actually talking.
- A setup page that walks you through it, including a live view of the I/O
  module so you can lift a float by hand and see which channel it is on.
- Email and SMS, with a test button on each that sends a real message, so you
  can prove the path works before you need it. Amazon SES and SNS are both
  supported, and any SMTP server works for email.

Next, in order:

- Recording every pump run: how long it ran, and what it actually drew once the
  starting surge had passed.
- Working out which pump is lead and which is lag, which the panel knows but
  does not tell anyone.
- The alert rules themselves, which decide when to use those channels.

## What you need

1. **A duplex pump panel** with dry contacts for the floats, the run signals and
   the motor overloads. The reference installation is a Magnus controller.
2. **A [Shelly EM Gen3](https://www.shelly.com/products/shelly-em-gen3)** with
   two current transformer clamps, one on each motor.
3. **A [ControlByWeb X-408](https://www.controlbyweb.com/x408/)**, on the same
   network. Eight optically isolated inputs, 4 to 26 V DC, which is what a 24 V
   panel gives you. Firmware 3.12 or newer, for MQTT.
4. **Somewhere to run Docker.** A NAS, a small server, a Raspberry Pi. It needs
   about 200 MB of memory and very little else.

Nothing here is polled. The Shelly is read over a websocket it pushes on. The
X-408 publishes to an MQTT broker when an input changes, and the broker comes up
alongside PitWatch in the same compose file. So a float lifting and a reading
changing both arrive the moment they happen, and there is no poll interval to
pick between a fast alarm and a busy network.

That is also why the X-408 opens the connection rather than answering one. The
only thing that has to be reachable on your network is the broker, and it is
running on the same machine as the dashboard.

The Shelly needs a fixed address, either static or a DHCP reservation: PitWatch
holds a connection open to it and reconnects when it drops, but it does not go
looking for a device that has moved. The X-408 does not, because it dials in;
give it whatever address your network hands out.

## Install

```sh
mkdir pitwatch && cd pitwatch
curl -O https://raw.githubusercontent.com/dkmcgowan/pitwatch/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/dkmcgowan/pitwatch/main/.env.example
mv .env.example .env
```

Put a database password and a broker password in `.env`. Those are the only
two settings without a sensible default; everything else is in
`docker-compose.yml` with a comment next to it.

```sh
docker compose up -d
```

Then open `http://<your-host>:8080` and sign in as **`admin`** with the password
**`pitwatch`**. It will make you change it before anything else opens, and then
walk you through setup.

The application and the broker run on the host's network, so your Shelly is
reachable at its own address with nothing in the way, and the X-408 can reach
the broker at this machine's address. If you would rather they did not, the
compose file says in a comment what to change.

If a device will not connect, check the host can reach it first, then that the
address is an IP rather than an mDNS name: `.local` names resolve on your
machine and not inside a container.

### Monitoring it

| Path | Answers | Returns |
| --- | --- | --- |
| `/health` | Is the process up and serving | `ok` as plain text, 200. Touches no database. GET or HEAD. |
| `/healthz` | Can it reach its database | JSON, 200 when it can, 503 when it cannot |

Point a load balancer or an uptime check at `/health`. It is deliberately cheap:
a proxy polling every couple of seconds should not become a database query every
couple of seconds. Uvicorn binds its socket only after startup finishes, which
includes connecting to Postgres and applying migrations, so a 200 from it means
the application genuinely came up.

`/healthz` answers the other question, and is worth asking less often. The
container's own `HEALTHCHECK` uses it, so `docker compose ps` already reflects
it.

## Accounts

Everything needs an account. The first boot creates `admin` with the password
`pitwatch`, because an appliance nobody can get into is useless, and then
refuses to open anything except the change password page until that password is
gone. It is published here, so it is known to everybody, and that is the only
thing that makes shipping it defensible.

### If it is reachable from the internet

Set these two. Neither is on by default, because both are wrong for somebody
running this on their own network, and both are necessary once it is not.

| Setting | Why |
| --- | --- |
| `PITWATCH_SECURE_COOKIES=true` | Marks the session cookie Secure, so a browser will not send it over plain HTTP. Set it when a proxy in front is terminating TLS. |
| `PITWATCH_TRUSTED_PROXIES` | Which addresses may say, through `X-Forwarded-For`, who the client is. It defaults to loopback. Set it to your proxy's address if the proxy is on another host, and never to `*`: anybody who can reach the port could then claim to be any address, which defeats the per address throttling. |

What is already true without configuring anything:

- **Passwords** are Argon2id, at the library's own parameters, and rehashed on
  sign in when those parameters change.
- **Signing in is throttled**, per user name and per client address, so neither
  guessing one account nor spraying many is cheap.
- **Every unsafe request needs a CSRF token.** The cookie is `SameSite=Lax`,
  which stops another site posting these forms, and there is a token as well,
  because Lax treats a neighboring subdomain as the same site and is a browser
  behavior rather than something this enforces.
- **Changing a password ends every other session** for that account, which is
  the point of changing it when you think a cookie was taken.
- **A disabled or deleted account stops working on its next request**, rather
  than whenever its cookie would have expired.
- **Security headers** on every response: a content security policy that allows
  no third party scripts, styles, frames or images at all, `frame-ancestors
  'none'`, `nosniff`, and a referrer policy that keeps invitation links out of
  other sites' logs.
- **Invitation and reset links** are single use, expire in three days, are
  stored only as a hash, and are invalidated when a new one is issued.
- **There is no self service password reset**, deliberately. Asking an
  administrator is one more step and it is also one fewer way to find out
  whether an address has an account here.

CI runs `pip-audit` against the pinned dependencies on every push, so a library
going bad is noticed without anything in this repository changing.

What is **not** here: two factor authentication, and any audit log beyond what
goes to the container log. Neither is hard to add and neither is pretending to
be there.

### Users

Everyone who should be told about a pump is an account on the **Users** page:
name, email address, mobile number, and whether they want email, texts, or both.
Most of them will never sign in. That is the normal case, and it is why a
password is optional here: the reason a building superintendent is in this list
is to get a text at two in the morning.

Anyone who also wants to watch the dashboard gets an invitation. Add them, tick
**email them a link to set a password**, and they get a message with a link that
works once and expires in three days. If email is not set up yet, the link is
shown on screen instead so you can send it yourself. Setting a password lets
them sign in and see live status; it does not make them an administrator, and
they cannot reach the settings.

Only administrators can change settings or manage users. An administrator
cannot remove their own rights, disable themselves, or delete their own account,
because an install with nobody who can change anything needs the database
editing by hand to recover.

### What a stranger can see

Almost nothing. The exceptions are the login page, the health checks, the static
files, the password link page, and the **messaging policy** and **privacy**
pages.

Those last two are public deliberately. A carrier reviewing a toll-free number
registration has to be able to read how people opt in and out of text messages
without an account, and so does anybody deciding whether to give you their phone
number. Putting consent terms behind a login is both a failed registration and
the wrong thing to do. The login page carries a summary of the same terms,
because that is the page an unauthenticated visitor actually lands on.

**Fill in the building and the contact details in settings before pointing a
carrier at those pages**, because they are what the pages say. Until the
building is set they read "PitWatch monitors this building's pumps", which is
true and vague; once it is set they read "the pumps at 822 Greenwich St".

`/messaging-policy` is the URL to register.

The application is called PitWatch everywhere. The **building** is a separate
thing: an address or a name, whichever somebody woken at two in the morning
would recognize. It goes in the subject line of every alert and on the policy
pages, and it has no default, so that a placeholder nobody chose never ends up
printed on a page a carrier is reading.

## Set it up

Setup runs in the browser once you have signed in. It asks for:

**The Shelly.** Its address, and which of its two clamps is on which pump. The
clamps are identical and the device has no idea which motor it is measuring, so
this is the one thing it cannot work out for itself. If you get it backwards,
pump 1 will show pump 2's current; swap it in the settings page.

**The panel inputs.** The broker to listen to, a name for each of the eight
inputs, and whether each one is on when voltage is **present** or when it is
**missing**. Get that second one backwards and an alarm reads as permanently on,
then goes quiet at the moment it matters, so check it against the panel rather
than guessing. The broker fields arrive already filled in from your `.env`.
See [Setting up the X-408](#setting-up-the-x-408) and
[Wiring the I/O module](#wiring-the-io-module) below.

**You can skip the panel inputs entirely to start with.** Untick the box. The
clamps go on in ten minutes; the I/O module needs the panel opened up, so
starting with current only is the normal way in rather than an edge case. You get live amps, per pump running state and the history straight
away. The contacts show as **not wired** and the module shows as **not set up**
rather than as a fault, and no rule that depends on a contact will fire on
nothing.

Add it later on the settings page. The reader starts on its own when you save,
without restarting the container.

**Name the inputs.** Each of the module's eight inputs gets a name you type,
and that is all there is to it. Give one a name and it is watched, recorded and
shown on the dashboard. Leave the name empty and nothing is wired there, so it
is left alone.

Name them after what is printed in the panel or on the wire. There is no list to
pick from, so anything your panel brings out can go on an input: the usual
floats and run contacts, or a seal failure, a phase monitor, a hand-off-auto
position. What a new install suggests, in the order a duplex ejector panel
usually brings them out:

| Input | Usually |
| --- | --- |
| DI1 | Lead float. The first float. The pump the controller calls lead starts on this. |
| DI2 | Lag float. The second float, higher up. The other pump joins in. |
| DI3 | High water alarm float. Higher still. Water is winning. |
| DI4 | Panel alarm contact. The controller's own generic alarm. |
| DI5 | Pump 1 running. Contactor closed on pump 1. |
| DI6 | Pump 2 running. Contactor closed on pump 2. |
| DI7 | Pump 1 overload tripped. |
| DI8 | Pump 2 overload tripped. |

Those are suggestions, not meanings. PitWatch keys everything on the input
number, so renaming one is safe at any time and changes only what you read.

**Your pumps.** The nameplate full load amps off each motor, and the current
above which a pump counts as running. That second number is not zero: a clamp on
a live conductor reads a little noise even when the motor is off.

## Email and SMS with AWS

Any SMTP server works for email, and any of them is easier than what follows.
This section is for Amazon SES and SNS specifically, because that is what the
reference installation uses.

### Email, through SES

SES speaks ordinary SMTP, so there is nothing AWS shaped about the settings.

1. In the SES console, **verify an identity**: either a domain, or just the one
   address you want mail to come from. That address goes in **From address**.
2. **Get SMTP credentials** under SES, Account dashboard, SMTP settings. The
   user name is an IAM access key id and the password is **derived** from the
   matching secret access key, not the secret key itself, so a secret key
   pasted straight in is refused. The console does that derivation when it
   offers to create credentials; deriving it yourself from an IAM key you
   already have is perfectly ordinary and works the same. The derivation
   includes the region, so a password made for one region will not send
   through another.
3. Fill in the settings:

   | Field | Value |
   | --- | --- |
   | Server | `email-smtp.<region>.amazonaws.com` |
   | Port | 587 |
   | Security | STARTTLS |
   | User name and password | The SMTP credentials from step 2 |
   | From address | The identity verified in step 1 |

4. Press **Send test email**.

A new SES account is in the sandbox, which can only send to verified addresses.
Verify your own address to test, and request production access when you want to
send to anyone else. If the test fails, the message says which of these it was.

### SMS, through SNS

**Read this before spending an evening on it.** Texting a US number is not
something you can turn on this afternoon. A new AWS account is in the SNS SMS
sandbox and can only reach numbers it has verified, and reaching US numbers at
all requires an **origination identity**: a registered 10DLC, a registered
toll-free number, or a short code. Registration takes days, costs money, and
asks about your business and what you intend to send.

That is a rule about US A2P messaging rather than anything peculiar to AWS.
Twilio and every other paid provider require the same registration, so switching
provider does not avoid it.

What that means in practice:

- **To test today**, verify your own phone number in the SNS console under
  Text messaging, sandbox destination numbers. You can then text yourself while
  still in the sandbox, which is enough to prove the whole path works.
- **To alert anyone else**, register a toll-free number, which is the least
  involved of the three, and request production access.

Then:

1. Create an IAM user whose only permission is `sns:Publish`. Not an
   administrator key. The policy is four lines:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       { "Effect": "Allow", "Action": "sns:Publish", "Resource": "*" }
     ]
   }
   ```

2. Put its access key and secret, and the region your number is registered in,
   into the SMS settings.
3. Put your registered number in **Origination number**.
4. Press **Send test text**.

Messages are sent as `Transactional`, which asks the carriers to prioritize
delivery and costs slightly more per message. A pump alarm is the definition of
transactional.

### The free alternative, and what it costs you

The **carrier email gateway** option sends a short email to an address like
`5551234567@vtext.com` and lets the carrier turn it into a text. No
registration, no cost, works this afternoon. It is also unauthenticated,
delivered whenever the carrier feels like it, and being withdrawn by most US
carriers. It is genuinely useful as a second, redundant path to a phone. It
should not be the only thing standing between you and a flooded basement.

It sends through the email settings, so those have to work first.

## Setting up the X-408

The module talks to PitWatch and PitWatch never talks to the module, so all of
this is typed into the X-408's own web page. Find it at its IP address and open
**Setup**.

### Point it at the broker

**General Settings**, then the **MQTT** tab at the bottom, then add a broker:

| Field | What to put |
| --- | --- |
| Hostname | The IP address of the machine running PitWatch |
| Port | `1883`, or whatever `MQTT_PORT` is in your `.env` |
| Client ID | `x408` |
| Username | `MQTT_USERNAME` from your `.env` |
| Password | `MQTT_PASSWORD` from your `.env` |
| Encrypted | Off |
| Keep Alive | `30` |
| Clean Session | On |
| Birth Topic | `pitwatch/status` |
| Birth Message | `online` |
| Last Will Topic | `pitwatch/status` |
| Last Will Message | `offline` |

The birth and last will pair is what makes the module going offline something
PitWatch is told about rather than something it has to notice. The module says
`online` when it connects, and it hands the broker the `offline` message to
publish on its behalf if the connection drops. Keep Alive is how long the
broker waits before deciding that has happened, so 30 seconds is the worst case
for hearing that the panel has lost power.

Type these two topics exactly. They also appear on the PitWatch settings page,
and the two have to agree; nothing will warn you if they do not, because a
topic nobody publishes to looks exactly like a device that has nothing to say.

### Tell it what to publish

Still under **MQTT**, add a publication:

| Field | What to put |
| --- | --- |
| Publication Name | `inputs` |
| Broker | the one you just added |
| Publish on Change | **On** |
| Publish Interval | leave empty |
| Topic | `pitwatch/inputs` |
| QoS | `1` |
| Retain | On |

and for **Payload**, all eight inputs in one line:

```
{"1":${digitalInput1},"2":${digitalInput2},"3":${digitalInput3},"4":${digitalInput4},"5":${digitalInput5},"6":${digitalInput6},"7":${digitalInput7},"8":${digitalInput8}}
```

Every message carries all eight, which matters more than it looks. One message
per changed input would mean PitWatch holding a picture assembled from
fragments, and a fragment lost across a reconnect would leave that picture
quietly wrong. This way any message that arrives is the whole truth, and a
message that does not arrive costs nothing but the next one.

**Publish on Change** is the setting that makes an alarm arrive in the time it
takes to cross the network. Leave the interval empty: there is nothing to say
on a timer that has not already been said. **Retain** means the broker keeps
the last message, so PitWatch reconnecting gets the current state at once
instead of waiting for the next float to move.

## Wiring the I/O module

> Turn the panel off first, and if you are not comfortable working inside a pump
> control panel, have an electrician do it. PitWatch only reads; nothing here
> should change how the panel behaves, and if it does, something is wired wrong.

The X-408's inputs are optically isolated and want **applied voltage**, 4 to
26 V DC. They do not supply their own sensing current, so a free dry contact
has to be given something to switch. On a 24 V panel that is the control supply
it is already sitting next to.

The eight inputs share four negative terminals, a pair of inputs to each. So
inputs 1 and 2 return through one terminal, 3 and 4 through the next, and so
on. Two signals on a pair have to reference the same common, which on a panel
with one control transformer they all do anyway. It is worth knowing before you
plan the wiring rather than after.

**For a signal the panel already energizes** (a control circuit where a live
line means the thing is happening), run the line to its input and the panel's
control common to that input's negative terminal. Use the **panel's** common,
not the X-408's own `Gnd`, or the isolation you are paying for stops isolating
anything.

**For a free dry contact**, put the contact in series between the control
supply and its input, with the negative terminal on the control common. The
contact closing puts voltage across the input, which is what it reads.

The inputs are specified for **DC**. If your control circuit is AC, the
optocoupler still conducts, but it drops out briefly at every zero crossing,
120 times a second on 60 Hz mains. The module's own 20 ms minimum hold covers
most of that, and the hold on the PitWatch settings page, half a second by
default, covers the rest: a gap that short never lasts long enough to count as
a change.

Each input draws between about 1 and 8.5 mA depending on the voltage, so eight
of them is a few tens of milliamps on the panel's control transformer. That is
usually nothing, but it is worth a thought if you are tapping a circuit that is
already close to its limit or is current limited for a reason.

The module itself takes 9 to 28 V DC on `+Vin` and `Gnd`, or Power over
Ethernet. PoE is the better answer here: one cable, and the module's power
comes from the same place as the network rather than from the panel it is
watching. A module fed by the panel goes quiet exactly when the panel loses
power, which is one of the things you want to be told about.

**Which way round is each input?** Use the live view on the settings page
rather than reasoning about it. It listens for the module to publish and shows
each input both raw and as it will be recorded. Lift a float by hand, or run a
pump, and watch which row changes and which way. Nothing appearing means
nothing moved: the module speaks only when something changes, so silence there
is an answer rather than a failure. Check the alarm and overload
contacts in particular: many panels hold those energized while everything is
fine and drop them on the fault, so that a cut wire reads as a fault rather than
as silence. Those are the inputs to set to **on when voltage is missing**.

**Alerts.** Where to send them. Both the email and the SMS sections have a
**send a test** box that sends a real message using whatever is in the boxes at
that moment, saved or not, so you can check a change before committing to it.
See [Email and SMS with AWS](#email-and-sms-with-aws).

## How it decides things

**Running or not.** A pump is running when current is above the threshold you
set. The panel's own run contact used to count too, and the disagreement
between the two was the useful part: a closed contactor with no current is a
motor that is not turning. Bringing that back needs a way to say which input
carries pump 1's run contact, and inputs are names you type rather than roles
this application knows. Undecided; see the note under [Alerts](#alerts).

**How often the clamps actually report, and what it costs.** The meter is not
sampled on a timer. It pushes when a reading changes and otherwise reports on
its own schedule, which on the reference panel worked out at about every
fifteen seconds. A run that lasts a few seconds produces two readings.

So the clamps say what a pump draws while running, and roughly how often it
runs. They **cannot time a run**, and the **run count is a floor rather than a
tally**: two runs close together can arrive looking like one, because a zero
that was never reported is not a zero that never happened. Reading the absence
of a report as the absence of an event is a mistake this made once, and the
operator standing in front of the panel is the reason it did not survive.

Run length, an exact count, and anything else that needs to see every
transition comes from the panel's run contact through the I/O module, which
publishes the moment the contact moves.

**Inrush is discarded, not smoothed** (designed, not yet built). A motor pulls
several times its running current as it comes up to speed. At the rate readings
arrive, that surge lands in the **first reading of a run and nowhere else**: on
the reference panel the first reading of each run ran about 1.3 A above every
later one. So the first reading of a run is dropped from the averages and from
the overcurrent check, which is what lets that threshold sit just above the
running current instead of above the surge. The peak is still recorded, on its
own, because a starting surge that climbs month over month is a bearing on the
way out.

That is also why the overcurrent check counts **readings rather than
milliseconds**. Two readings in a row above the threshold is about half a minute
at the meter's own pace, and it would be two seconds if anything ever sampled
faster. A hold measured in milliseconds asked for readings that do not exist.

**Lead and lag** (designed, not yet built). The controller alternates: it starts
one pump this time and the other next time, so wear is even. Its display says
`P1:Lead P2:Lag` and then flips. It does not put that on a contact anywhere, so
PitWatch works it out. Whichever pump started first last cycle was lead, so the
other one is lead next. A high water call, where both pumps start more or less
together, does not update the assignment, because there is no first pump to
read.

**Amps, and nothing derived from voltage.** The dashboard shows current and
does not show volts, watts or power factor. That is not an omission.

A current transformer measures the field around a conductor. It gives you the
current in that conductor whatever the meter happens to be using as a voltage
reference, so amps are a real measurement and can be trusted.

Real power cannot be. Watts and power factor need voltage and current from the
same phase, with the correct angle between them. In the reference installation
the Shelly is powered from an ordinary 120 V outlet rather than tapped off one
of the phases its clamps are around, so the angle between what it is measuring
and what it is referencing is arbitrary. The magnitudes look plausible, which
is exactly what makes reporting them a bad idea.

Those fields are still recorded, in case the reference is ever moved onto a
measured phase, but nothing displays them until it is.

**Only one phase is measured.** The clamps go on L1 of each motor. A three phase
figure derived from one phase assumes balanced phases and will not notice a
single phasing fault, so anything of that kind is labeled as derived wherever it
appears.

## Alerts

**Configured, not yet firing.** The rules, their wording and every threshold
they use live on the **Alerts** page under Settings. Nothing raises them yet;
the engine that watches for them is the next piece.

Each rule has four things you can set: whether it runs, how loudly (info,
warning or critical), whether it goes to administrators only, and what it
says. Everybody whose own level on the Users page is at or below the rule's
level hears it.

**One message, two lengths.** The line you write is what a text message says,
so it wants to be short enough to read on a lock screen. An email sends the
same line and then the readings behind it, and adds a link to the dashboard
for anybody who has a password. You never write it twice.

Placeholders in braces are filled in when it sends. Anything unrecognized is
left alone, because a typo in a message should produce a slightly odd alert
rather than no alert at all.

| Alert | Reads | What it means |
| --- | --- | --- |
| High water | contacts | The float above the lag float is wet. The message says whether both pumps are running, because coping and not coping are different nights. |
| Panel alert, unexplained | contacts | The controller's own alarm, which carries no detail. Waits a few seconds to see whether something that does carry detail explains it, and stays quiet if one does. |
| Overload tripped | contacts | A pump is off and stays off. The default wording assumes hand reset, which is what the red button is for. |
| Switched on, drawing nothing | both | The run contact is closed and the clamp reads zero. The motor is not turning. Neither sensor can see this alone. |
| Drawing too much | clamps | Above the threshold for several readings, so this is not the starting surge. Set per motor. |
| Ran too long | contacts | A stuck float or a blockage. Off until there is a number behind it. |
| Short cycling | clamps | Restarting unusually soon after stopping, several times running. Usually a check valve letting the discharge run back into the pit. |
| Nothing has run | clamps | Either a dry spell or a blind monitor. |
| Drawing more than it used to | clamps | The steady draw climbing week over week, which is the reason typical load exists. Nothing is ever wrong on the day. |
| A device stopped answering | PitWatch | Administrators only by default: worth waking somebody who can fix it, noise to everybody else. |
| Float activity | contacts | Every float, every time. Off by default; on a working pit this fires several times an hour. |
| A pump started | contacts | Every run. Off by default, for the same reason and useful for the same reason. |

Each one is raised once and stays open until the condition goes away, so a
float that chatters twenty times sends one message rather than twenty, and you
get an all clear when it drains.

**Several of these cannot fire until the I/O module is wired**, and the page
says which. A rule that silently never runs looks exactly like a rule that
never found anything.

## Where the data lives

In a Docker named volume, not in the folder you ran `docker compose` from.
Nothing is written next to the compose file, which is why there is no database
file to find there.

```sh
docker volume ls | grep pitwatch
docker volume inspect pitwatch_pitwatch-db
```

The volume is `pitwatch_pitwatch-db`: Compose prefixes the volume name from the
compose file with the project name. On Linux it lives under
`/var/lib/docker/volumes/pitwatch_pitwatch-db/_data`. Do not edit it by hand
while the stack is running; it is a live Postgres data directory.

Pulling a newer image keeps it: `docker compose pull && docker compose up -d`
replaces the container, reattaches the same volume, and runs any new migrations
on start.

**`docker compose down` keeps the volume. `docker compose down -v` deletes it**,
along with every reading, and there is no undo. That flag is the only routine
way to lose the data.

### Taking it down and bringing it back

`docker compose down` followed by `docker compose up -d` reuses the same volume.
Compose matches it by project name and volume name, both of which are fixed in
the compose file, so it reattaches whatever was there: your history, your
settings, and your account. You will not be sent back through setup, and you do
not need to reconfigure the devices.

Because the project name is set explicitly in the file rather than taken from
the directory, this holds even if you rename or move the folder you run it from.

Two ways to lose it by accident:

- **`docker volume prune --all`**, when the stack is down. Since Docker Engine
  23.0 a plain `docker volume prune` only removes anonymous volumes, so it will
  not touch this one; it is `--all` that includes named volumes, and on Docker
  older than 23.0 the plain form does too. Either way "unused" means not
  referenced by any container, so the exposure is only while the stack is down.
  See [Cleaning up](#cleaning-up).
- **Changing `POSTGRES_PASSWORD` after the first start.** The Postgres image
  only applies that on an empty data directory, so the stored password stays
  what it was and the application starts failing to authenticate against its own
  database. The symptom is the app container retrying forever. Either put the
  old password back, or change it properly with `ALTER ROLE pitwatch PASSWORD
  '...'` inside `psql` and then update `.env` to match.

### Cleaning up

Two different jobs, and the commands are not interchangeable.

**To start PitWatch over**, use the scoped command rather than a prune. It
removes this project's containers, network and volumes and touches nothing else
on the machine:

```sh
docker compose down -v
docker compose up -d
```

Add `--rmi all` to drop the images too, if you want the next `up` to pull fresh.

That means going through setup again, and it is the only way to genuinely reset.

**To tidy up unused volumes across the whole machine**, do it with everything
you care about running. A volume attached to any container, running or stopped,
does not count as unused, so anything that is up is protected. Look before you
leap:

```sh
docker volume ls -f dangling=true   # exactly what a prune would take
docker volume prune -a              # take them, named volumes included
```

The `-a` matters in both directions. Without it, on Docker 23.0 and newer, a
prune only removes anonymous volumes and leaves named ones such as
`pitwatch_pitwatch-db` alone. With it, an unused named volume is fair game. So
the safe habit is to bring your stacks up first, then prune, and to read the
`dangling=true` list rather than trusting either default.

### Putting it in the compose folder instead

If you would rather see it next to the compose file, swap the volume for a bind
mount on the `db` service:

```yaml
    volumes:
      - ./data/db:/var/lib/postgresql/data
```

and drop the top level `volumes:` block. It works, and the trade is real: the
directory ends up owned by the container's postgres user rather than by you, so
it is more awkward to move and to back up, and on Docker Desktop for Mac and
Windows a bind mounted Postgres directory is noticeably slower and occasionally
unhappy. The named volume is the default for those reasons. Either way, the way
to take a copy is a dump rather than copying files out from under a running
database.

## Backing it up

A dump is the thing to keep, not a copy of the volume. It is portable across
Postgres versions and it is consistent, which a file copy of a running database
is not.

```sh
docker compose exec -T db pg_dump -U pitwatch pitwatch | gzip > pitwatch-$(date +%F).sql.gz
```

To restore into an empty stack:

```sh
gunzip -c pitwatch-2026-08-25.sql.gz | docker compose exec -T db psql -U pitwatch pitwatch
```

Raw one second readings are kept for 90 days, compressed after the first week.
Minute averages are kept for a bit over a year, hourly averages indefinitely. So
a year from now you can still ask what pump 1 usually drew last spring, without
the database being large.

---

## Developing

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check pitwatch tests scripts
pytest
```

`scripts/check_style.py` enforces the two prose rules CI cares about: American
spellings, and no typographic punctuation in tracked files.

Most of the test suite needs a database and skips itself without one. The tests
that matter most are in that group, because the migrations create hypertables,
continuous aggregates and retention policies, and nothing short of a real
TimescaleDB can tell you whether they work. Point the suite at one and it runs
them:

```sh
docker compose up -d db
export PITWATCH_TEST_DATABASE_URL=postgresql://pitwatch:PASSWORD@localhost:5432/pitwatch
pytest
```

CI does exactly this against a Timescale service container, and sets
`PITWATCH_REQUIRE_DATABASE=1` so that a missing database is a failure there
rather than a quiet skip. The test database has its schema dropped and rebuilt
between tests, so do not point it at anything you want to keep.

To run the application itself against a local database without building the
image:

```sh
docker compose up -d db
PITWATCH_DATABASE_URL=postgresql://pitwatch:PASSWORD@localhost:5432/pitwatch python -m pitwatch
```

Both of those need the `ports` block on the `db` service uncommented.

Migrations are plain SQL in `pitwatch/migrations`, applied in name order at
startup and recorded in `schema_migration`. There are no down migrations.

## Container images

You do not need to build anything. Images are built by GitHub Actions and
published to `ghcr.io/dkmcgowan/pitwatch`, public and pullable without signing
in, for `linux/amd64` and `linux/arm64`.

| Tag | What it is |
| --- | --- |
| `latest` | The tip of `main`. Moves whenever something is merged. |
| `main` | The same image, named after the branch. |
| `0.2` | The newest patch release on that minor line. |
| `0.2.2` | One release. Never moves. |

The compose files use `latest`, which is right while you are setting this up:
a fix is then one `docker compose pull && docker compose up -d` away. **Pin a
version once it is watching a real pump.** A tag that moves under you is not
what you want standing between a basement and a flood, and `0.2` is a good
middle ground: patch fixes arrive, nothing else does.

Whichever you choose, a pull only gets you something new if the tag you asked
for has moved. Pulling a pinned version and wondering why the fix did not
arrive is a very easy afternoon to have.

To check what is actually running, ask it:

```sh
curl -s http://<your-host>:8080/healthz
```

It answers with the version. The page footer shows it too.

A push to `main` builds `latest` and `main`. Pushing a `v*` git tag additionally
builds the version tags:

```sh
git tag -a v0.2.3 -m "Version 0.2.3"
git push origin v0.2.3
```

To build it yourself instead, the compose file has a commented `build: .` on the
app service. Swap it for the `image:` line and `docker compose build`.

### The database image

The compose files use `timescale/timescaledb:latest-pg17`, which floats
Timescale releases while pinning the Postgres major version at 17. That pin is
deliberate and worth keeping.

Plain `latest` would follow Postgres major versions too. Postgres refuses to
start on a data directory written by an older major, so the day that tag moved
from 17 to 18 the database would stop coming up, and getting it back would mean
a dump and restore rather than a rollback. `latest-pg17` cannot do that to you.

It is also not the `-oss` tag. Compression, retention policies and continuous
aggregate refresh are Timescale Community License features that the open source
build does not have, so the rollups in migration 003 would fail on it.

When a newer Timescale image does arrive, its binaries are newer than the
extension registered inside your database. PitWatch notices at startup and runs
`ALTER EXTENSION timescaledb UPDATE` for you, on its own connection before
anything else touches the database, which is the only place that statement is
allowed to run. If it cannot, it says so in the log and carries on rather than
refusing to start, on the grounds that a monitor that will not run is worse than
one running on a slightly older extension.

## License

MIT.
