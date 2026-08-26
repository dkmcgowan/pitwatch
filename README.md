# PitWatch

Monitoring and real alerting for a duplex ejector pump panel.

Most pump controllers have one alarm contact and one thing to say with it:
something is wrong. Not which pump, not how wrong, not whether it has happened
before. PitWatch watches the same panel with a pair of current clamps and an
Ethernet I/O module and turns that one contact into a page that says *pump 2 has
been drawing 14.2 A for four minutes, the high water float is wet, and both
pumps are running*.

> **Early, and not yet proven against real hardware.** Setup, both device
> readers and the live dashboard are written and tested, including against a
> real TimescaleDB in CI. Neither reader has yet talked to an actual Shelly or
> an actual Waveshare, and the I/O module in the reference installation is not
> wired up. This paragraph gets edited when that stops being true.

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
3. **A Waveshare 8 channel Ethernet I/O module**, Modbus TCP, on the same
   network.
4. **Somewhere to run Docker.** A NAS, a small server, a Raspberry Pi. It needs
   about 200 MB of memory and very little else.

The Shelly is read over a websocket it pushes on, so a reading appears the
moment it changes.

The Waveshare is polled, five times a second by default. That is not a shortcut
taken to save effort: Modbus is a master and slave protocol and a slave is never
allowed to speak first, in RTU or in TCP. The connection is held open, which
saves the handshake and nothing else. The module's MQTT mode does not help
either; it carries Modbus frames rather than publishing events, so it would be
the same poll with a broker added to the alarm path. Eight bits at five times a
second is about a hundred bytes a second, and the worst case delay in noticing a
float is one poll interval.

Both devices need a fixed address, either static or a DHCP reservation. PitWatch
holds a connection open to the Shelly and reconnects when it drops, but it does
not go looking for a device that has moved.

## Install

Two files, and one of them you copy from this repository rather than retyping.

**1. Pick a compose file.** They differ in one thing: how the container reaches
the network.

| File | Use it when |
| --- | --- |
| `docker-compose.yml` | The normal case. The application runs on its own network and reaches your devices through the host. |
| `docker-compose.host.yml` | The container cannot reach your Shelly or Waveshare, which shows up as the test button timing out on a device your host can ping. Also what you want if a reverse proxy on this host is fronting it. |

Start with the first. If the test button times out, switch to the second; they
share a project name and a volume, so your data comes with you.

```sh
mkdir pitwatch && cd pitwatch
curl -O https://raw.githubusercontent.com/dkmcgowan/pitwatch/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/dkmcgowan/pitwatch/main/.env.example
mv .env.example .env
```

**2. Edit `.env`.** Only one line has to change:

```sh
POSTGRES_PASSWORD=pick-something-here
```

Everything else in that file has a working default and is there to be changed
when you need it, not before. The two worth knowing about now:

```sh
# Set both of these if a reverse proxy in front is terminating TLS.
PITWATCH_SECURE_COOKIES=true
PITWATCH_TRUSTED_PROXIES=10.0.0.2      # the address your proxy connects from
```

**3. Start it.**

```sh
docker compose up -d
```

Then open `http://<your-host>:8080` and sign in as **`admin`** with the password
**`pitwatch`**. It will make you change it before anything else opens, and then
walk you through setup.

For the host networking file, every command takes `-f docker-compose.host.yml`:

```sh
docker compose -f docker-compose.host.yml up -d
```

### What is in .env

| Setting | Default | What it does |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | **required** | The database password. There is no default on purpose. |
| `PITWATCH_HOST_PORT` | 8080 | The port to reach PitWatch on. Bridge setup only. |
| `PITWATCH_PORT` | 8080 | The port the application binds. Host networking setup only, where there is no mapping to move. |
| `POSTGRES_HOST_PORT` | 5432 | Where Postgres is published. Only used by the host networking setup, and by the commented out block in the other. |
| `PITWATCH_TIMEZONE` | America/New_York | For every timestamp shown. Storage is always UTC. |
| `PITWATCH_SECURE_COOKIES` | false | Marks the session cookie Secure. Set it behind a TLS proxy. |
| `PITWATCH_TRUSTED_PROXIES` | 127.0.0.1,::1 | Which addresses may say who the client is. Set it to your proxy. Never `*`. |
| `PITWATCH_LOG_LEVEL` | INFO | DEBUG logs every reading the Shelly pushes, which is a line a second. |
| `SHELLY_HOST`, `WAVESHARE_HOST` | empty | Optional. Seeds the device addresses on a first boot so the wizard has less to ask. Read once, while the settings are empty. |

The database is not published on a host port in the bridge setup. The
application reaches it as `db:5432`, container to container, and nothing outside
the stack needs to. There is a commented out `ports` block on the `db` service
if you want to point psql or pgAdmin at it.

### When the container cannot reach your devices

The usual symptom is the **Test connection** button timing out on a device your
Docker host can ping perfectly well. The test button walks up the stack and
tells you which rung it fell off, so read that first: whether the name resolved,
whether a TCP connection opened, whether HTTP answered, and whether the
websocket did.

Two causes account for almost all of it.

**An mDNS name.** If you entered something ending in `.local`, your machine
resolves it by multicast and a container cannot, because Docker's DNS does not
do multicast. Use the IP address, and give the device a DHCP reservation so it
keeps it.

**The container cannot route to your LAN.** A timeout rather than a refusal
usually means this: a host firewall dropping forwarded traffic off the Docker
bridge, a device on an isolated Wi-Fi network, or a bridge subnet that overlaps
your LAN. Rather than working out which, put the application on the host's
network, where it has exactly the reachability the host has:

```sh
docker compose -f docker-compose.host.yml up -d
```

That file is in the repository and handles the two things that change with host
networking: there is no port mapping any more, so set `PITWATCH_PORT` rather
than `PITWATCH_HOST_PORT`, and the application reaches the database on
`127.0.0.1` instead of by service name. The database is published on loopback
only, so nothing on your LAN can reach Postgres.

`POSTGRES_HOST_PORT` moves the database off 5432 if that is taken on the host.
In this setup it is not only for outside tools: it is the port the application
itself dials, so the mapping and the connection string both read that one
variable and cannot drift apart.

### Monitoring it

Two endpoints, answering two different questions.

| Path | Answers | Returns |
| --- | --- | --- |
| `/health` | Is the process up and serving | `ok` as plain text, 200. Never touches the database. GET or HEAD. |
| `/healthz` | Can it reach its database | JSON, 200 when it can, 503 when it cannot |

Point a load balancer or an uptime check at **`/health`**. It is deliberately
cheap: a proxy polling every couple of seconds should not turn into a database
query every couple of seconds, forever.

It is not as weak a check as it looks. Uvicorn binds its socket only after
startup has finished, which includes connecting to Postgres and applying
migrations, so a 200 from `/health` means the application genuinely came up.
While it is starting, or once it has died, the connection is refused, which
every checker already treats as down.

Use `/healthz` when you want to know whether it can currently do its job, and
poll it less often. The container's own `HEALTHCHECK` uses it, so
`docker compose ps` already reflects it.

For HAProxy:

```haproxy
backend pitwatch
    option httpchk GET /health
    http-check expect string ok
    option forwardfor
    # HAProxy does not add this on its own, and PitWatch has no other way to
    # know the browser is on https. Without it the sign in throttling counts
    # every request as coming from the proxy, and anything that has to name its
    # own address gets it wrong.
    http-request set-header X-Forwarded-Proto https
    server pitwatch 10.0.0.5:8080 check inter 5s fall 3 rise 2
```

`http-check expect string ok` rather than only a status code, so that something
else answering on that port with a cheerful 200 does not read as PitWatch being
up.

`option forwardfor` and the `X-Forwarded-Proto` line are both worth having.
PitWatch honors them from the addresses named in `PITWATCH_TRUSTED_PROXIES` and
ignores them from anywhere else.

Nothing breaks without them, because the pages reference their own assets by
path rather than by URL and the browser supplies the scheme. What suffers is
anything that has to state an address in full: put the public address in
settings so invitation emails carry a link that works.

### Changing the ports

Set `PITWATCH_HOST_PORT` in `.env` and `docker compose up -d`. That moves only
the host side; the container keeps listening on 8080, which is what its health
check expects.

`POSTGRES_HOST_PORT` does the same for the database, and matters only where the
database is published at all: always in the host networking setup, and in the
bridge setup only if you uncomment its `ports` block. In the bridge setup the
application always reaches Postgres as `db:5432` regardless.

There is a separate `PITWATCH_PORT` that changes the port the application itself
binds to, inside the container. You almost never want it. It matters in two
cases: running with `network_mode: host`, where there is no port mapping to do
the moving, and running `python -m pitwatch` directly without Docker. Setting
both to different values is how you end up with a mapping to a port nothing is
listening on.

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

### People

Everyone who should be told about a pump is a person on the **People** page:
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

Only administrators can change settings or manage people. An administrator
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

**The Waveshare.** Its address, and what each of the eight inputs is wired to.
Each channel has an **invert** switch next to it. Get this wrong and an alarm
reads as permanently on, then goes quiet at the moment it matters, so it is
worth checking against the panel rather than guessing. See
[Wiring the I/O module](#wiring-the-io-module) below.

**You can skip the Waveshare entirely to start with.** Leave its box unticked
and its address empty. The clamps go on in ten minutes; the I/O module needs the
panel opened up, so starting with current only is the normal way in rather than
an edge case. You get live amps, per pump running state and the history straight
away. The contacts show as **not wired** and the module shows as **not set up**
rather than as a fault, and no rule that depends on a contact will fire on
nothing.

Add it later on the settings page. The reader starts on its own when you save,
without restarting the container.

The signals PitWatch understands:

| Signal | What it means |
| --- | --- |
| Lead float | The first float. The pump the controller calls lead starts on this. |
| Lag float | The second float, higher up. The other pump joins in. |
| High water alarm float | Higher still. Water is winning. |
| Panel alarm contact | The controller's own generic alarm. |
| Pump 1 running | Contactor closed on pump 1. |
| Pump 2 running | Contactor closed on pump 2. |
| Pump 1 overload tripped | The motor overload relay for pump 1. |
| Pump 2 overload tripped | The motor overload relay for pump 2. |

You do not need all eight. Anything you leave as **Not connected** is simply not
watched, and the rules that depend on it stay quiet rather than firing on
nothing.

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
2. **Create SMTP credentials** under SES, Account dashboard, SMTP settings. This
   is the step people get wrong: SES SMTP credentials are generated separately
   and are **not** an IAM access key and secret, though they look almost
   identical. Pasting an IAM key in is the usual reason authentication fails.
3. Fill in the settings:

   | Field | Value |
   | --- | --- |
   | Server | `email-smtp.<region>.amazonaws.com` |
   | Port | 587 |
   | Security | STARTTLS |
   | User name and password | The SES SMTP credentials from step 2 |
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

## Wiring the I/O module

> Turn the panel off first, and if you are not comfortable working inside a pump
> control panel, have an electrician do it. PitWatch only reads; nothing here
> should change how the panel behaves, and if it does, something is wired wrong.

The Waveshare has `DI1` to `DI8` for the eight signals, plus two common
terminals, `DICOM` and `DGND`. What you connect `DICOM` to is what decides how
the inputs behave, and it is the single decision to get right:

| `DICOM` | Input type | Reads as on when |
| --- | --- | --- |
| Left floating | Dry contact, passive | The contact closes |
| To the supply negative | PNP, high level trigger | Voltage is present on `DIn` |
| To the supply positive | NPN, low level trigger | `DIn` is pulled down |

**If your panel signals voltage** (a control circuit where a live line means the
thing is happening), tie `DICOM` to that control supply's **common or negative**
and run each signal to its own `DIn`. This is the PNP row: voltage present reads
as on, which is what you want. Tie the common to the **panel's** common, not to
the Waveshare's own power ground, or the optocoupler isolation you are paying
for stops isolating anything.

**If your panel gives you free dry contacts**, leave `DICOM` disconnected and
run each contact between `DIn` and `DGND`. The module supplies its own sensing
current and stays isolated from whatever else the contact is doing.

The inputs accept **5 to 36 V**, and the wet contact modes are specified for
**DC**. If your control circuit is AC, the bidirectional optocoupler still
conducts, but it drops out briefly at every zero crossing, 120 times a second on
60 Hz mains. A poll can land in one of those gaps and read a live signal as off.
Leave every channel's debounce at a few hundred milliseconds and that
disappears, because the next poll disagrees and the change is discarded. Do not
set a channel to zero debounce on an AC circuit.

Each input draws a few milliamps from whatever supplies it, so eight of them is
a few tens of milliamps on the panel's control transformer. That is usually
nothing, but it is worth a thought if you are tapping a circuit that is already
close to its limit or is current limited for a reason.

**Which way round is each signal?** Use the live view on the settings page
rather than reasoning about it. It reads the module twice a second and shows
each input both raw and after the invert setting. Lift a float by hand, or run a
pump, and watch which row changes and which way. In particular, check whether
your alarm and overload signals are **fail safe**: many panels hold those
asserted while everything is fine and drop them on the fault, so that a cut wire
reads as a fault rather than as silence. Those are the channels that need
**invert** ticked.

**Alerts.** Where to send them. Both the email and the SMS sections have a
**send a test** box that sends a real message using whatever is in the boxes at
that moment, saved or not, so you can check a change before committing to it.
See [Email and SMS with AWS](#email-and-sms-with-aws).

## How it decides things

**Running or not.** A pump is running when current is above the threshold you
set, or when its run contact is closed. Both, ideally. When only one of the two
says so, that is itself worth telling you about: a closed contactor with no
current is a motor that is not turning.

**What a run actually drew** (designed, not yet built). A motor pulls six to
eight times its running current for a fraction of a second when it starts.
Averaging that in makes every healthy pump look overloaded, so the first couple
of seconds of each run are left out of the average. The peak is still recorded,
on its own, because a starting surge that climbs month over month is a bearing
on the way out.

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

**Not built yet.** This is the design, and the settings pages already collect
what it needs.

| Alert | Fires when |
| --- | --- |
| Overload tripped | The panel's overload contact opened for that pump. |
| Running over current | A pump has drawn more than its threshold for long enough that this is not a surge. |
| Running under current | A pump is running but barely drawing anything: an impeller spinning in air, a lost coupling, a lost phase. |
| High water | The high water float is wet. |
| Both pumps running | Normal during a lag call, worth knowing about the rest of the time. |
| Run too long | One run past the limit you set. Stuck float, or pumping against something. |
| Short cycling | Too many starts in an hour. Usually a check valve letting the discharge run back into the pit. |
| Contactor without current | The run contact closed and no current followed. |
| Current without contactor | Current with no run contact. Either a miswired channel or a contactor that is welded shut. |
| Device offline | The Shelly or the Waveshare stopped answering. |
| Nothing has run | No pump run at all for as long as you set. Either a very dry week or a sensor that has quietly died. |

Each one is raised once and stays open until the condition goes away, so a float
that chatters twenty times sends one message rather than twenty, and you get an
all clear when it drains.

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

Both compose files use the same project name and the same volume, so switching
between the bridge setup and the host networking one keeps your history. So does
pulling a newer image: `docker compose pull && docker compose up -d` replaces
the container and reattaches the same volume, and migrations run on start.

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
docker compose -f docker-compose.host.yml down -v --remove-orphans
docker compose -f docker-compose.host.yml up -d
```

Use the same `-f` file you brought it up with. `--remove-orphans` is worth
having because both compose files share one project name, so switching between
the bridge setup and the host networking one can leave the other mode's
container behind. Add `--rmi all` to drop the images too, if you want the next
`up` to pull fresh.

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
