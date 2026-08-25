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
    image: ghcr.io/dkmcgowan/pitwatch:latest
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    environment:
      PITWATCH_DATABASE_URL: postgresql://pitwatch:${POSTGRES_PASSWORD}@db:5432/pitwatch
      PITWATCH_TIMEZONE: ${PITWATCH_TIMEZONE:-America/New_York}
    ports:
      - "${PITWATCH_PORT:-8080}:8080"

volumes:
  pitwatch-db:
```

```sh
# .env
POSTGRES_PASSWORD=pick-something-here
PITWATCH_PORT=8080
PITWATCH_TIMEZONE=America/New_York
```

```sh
docker compose up -d
```

Then open `http://<your-host>:8080` and follow the setup.

The database is not published on a host port, on purpose. Nothing outside the
stack needs to reach it.

## Set it up

Setup runs in the browser the first time you open PitWatch. It asks for:

**The Shelly.** Its address, and which of its two clamps is on which pump. The
clamps are identical and the device has no idea which motor it is measuring, so
this is the one thing it cannot work out for itself. If you get it backwards,
pump 1 will show pump 2's current; swap it in the settings page.

**The Waveshare.** Its address, and what each of the eight inputs is wired to.
Each channel has a **normally closed** switch next to it. Get this wrong and an
alarm reads as permanently on, then goes quiet at the moment it matters, so it
is worth checking against the panel with a meter rather than guessing.

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

## Backing it up

Everything is in the `pitwatch-db` volume.

```sh
docker compose exec -T db pg_dump -U pitwatch pitwatch | gzip > pitwatch-$(date +%F).sql.gz
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

## License

MIT.
