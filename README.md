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

Next, in order:

- Recording every pump run: how long it ran, and what it actually drew once the
  starting surge had passed.
- Working out which pump is lead and which is lag, which the panel knows but
  does not tell anyone.
- Alerts, and sending them by email and SMS.

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

Make a directory, put these two files in it, and start it.

```yaml
# docker-compose.yml
name: pitwatch

services:
  db:
    image: timescale/timescaledb:2.29.2-pg17
    restart: unless-stopped
    environment:
      POSTGRES_DB: pitwatch
      POSTGRES_USER: pitwatch
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
      TIMESCALEDB_TELEMETRY: "off"
    volumes:
      - pitwatch-db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U pitwatch -d pitwatch"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s

  app:
    image: ghcr.io/dkmcgowan/pitwatch:0.1.1
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    environment:
      PITWATCH_DATABASE_URL: postgresql://pitwatch:${POSTGRES_PASSWORD}@db:5432/pitwatch
      PITWATCH_TIMEZONE: ${PITWATCH_TIMEZONE:-America/New_York}
    ports:
      - "${PITWATCH_HOST_PORT:-8080}:8080"

volumes:
  pitwatch-db:
```

```sh
# .env
POSTGRES_PASSWORD=pick-something-here
PITWATCH_HOST_PORT=8080
POSTGRES_HOST_PORT=5432
PITWATCH_TIMEZONE=America/New_York
```

```sh
docker compose up -d
```

Then open `http://<your-host>:8080` and follow the setup.

The database is not published on a host port, on purpose. The application
reaches it as `db:5432`, container to container, and nothing outside the stack
needs to. There is a commented out `ports` block on the `db` service if you want
to point psql or pgAdmin at it; `POSTGRES_HOST_PORT` sets which host port that
uses, for when 5432 is already taken.

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

## Set it up

Setup runs in the browser the first time you open PitWatch. It asks for:

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

**Alerts.** Where to send them. Email needs an SMTP server; SMS needs a
provider.

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

**Only one phase is measured.** In the reference installation the clamps are on
L1 of each motor. Three phase power figures derived from one phase are labeled
as derived, everywhere they appear, because they are an estimate that assumes
balanced phases and will not notice a single phasing fault.

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
| `0.1.0` | A tagged release. Never moves. |
| `0.1` | The newest patch release on that minor line. |

Pin a release tag on anything you are relying on. `latest` is fine for trying it
out and is exactly the wrong thing to leave a pump alarm running on, because it
changes under you.

A push to `main` builds `latest` and `main`. Pushing a `v*` git tag additionally
builds the version tags:

```sh
git tag -a v0.2.0 -m "Version 0.2.0"
git push origin v0.2.0
```

To build it yourself instead, the compose file has a commented `build: .` on the
app service. Swap it for the `image:` line and `docker compose build`.

## License

MIT.
