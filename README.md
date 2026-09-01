# PitWatch

Monitoring and real alerting for a duplex ejector pump panel.

Most pump controllers have one alarm contact and one thing to say with it:
something is wrong. Not which pump, not how wrong, not whether it has happened
before. PitWatch watches the same panel with a pair of current clamps and an
Ethernet I/O module, and turns that one contact into a page that says *pump 2
has been drawing 14.2 A for four minutes, the high water float is wet, and both
pumps are running*.

## What it does

- Reads running current from both motors continuously, over a websocket the
  Shelly pushes to, and keeps the history.
- Reads the floats, the run contacts and the overload contacts from the panel's
  own dry contacts.
- A live dashboard: both pumps, the controller's screen and the panel's own
  lamps in one view, the same on a phone as on a desktop.
- A history page: load, starts and the panel contacts over a day, a week or a
  month.
- Email and SMS, with a test button on each that sends a real message, so you
  can prove the path works before you need it.
- A written summary, for administrators, if you add an OpenAI key. It sends a
  week of figures and the description of the system you wrote in settings, and
  reads them back as a few paragraphs. Nothing is sent until somebody presses
  the button.

The alert rules are configured but do not fire yet. The engine that watches for
them is the next piece.

## What you need

1. **A duplex pump panel** with dry contacts for the floats, the run signals and
   the motor overloads. The reference installation is a Magnus controller.
2. **A [Shelly EM Gen3](https://www.shelly.com/products/shelly-em-gen3)** with
   two current transformer clamps, one on each motor.
3. **A [ControlByWeb X-408](https://www.controlbyweb.com/x408/)** on the same
   network. Eight optically isolated inputs, 4 to 26 V DC. Firmware 3.12 or
   newer, for MQTT.
4. **Somewhere to run Docker.** A NAS, a small server, a Raspberry Pi.

Nothing is polled. The Shelly pushes over a websocket; the X-408 publishes to an
MQTT broker when an input changes, and the broker comes up alongside PitWatch in
the same compose file. So there is no poll interval to pick between a fast alarm
and a busy network.

The Shelly needs a fixed address, static or a DHCP reservation. The X-408 does
not, because it dials in.

## Install

```sh
mkdir pitwatch && cd pitwatch
curl -O https://raw.githubusercontent.com/dkmcgowan/pitwatch/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/dkmcgowan/pitwatch/main/.env.example
mv .env.example .env
```

Put a database password and a broker password in `.env`. Those are the only two
settings without a sensible default; everything else is in `docker-compose.yml`
with a comment next to it.

```sh
docker compose up -d
```

Open `http://<your-host>:8080` and sign in as **`admin`** with the password
**`pitwatch`**. It makes you change that before anything else opens, then walks
you through setup. Follow the wizard.

If a device will not connect, check the host can reach it, then that the address
is an IP rather than an mDNS name: `.local` names resolve on your machine and
not inside a container.

## Accounts

Everyone who should be told about a pump is an account on the **Users** page.
Most of them never sign in; a password is optional, because the reason a
superintendent is in this list is to get a text at two in the morning. Anyone
who also wants the dashboard gets an invitation link. Only administrators can
change settings or manage users.

If this is reachable from the internet, set two things in `docker-compose.yml`:

| Setting | Why |
| --- | --- |
| `PITWATCH_SECURE_COOKIES=true` | Marks the session cookie Secure, so a browser will not send it over plain HTTP. Set it when a proxy in front terminates TLS. |
| `PITWATCH_TRUSTED_PROXIES` | Which addresses may say, through `X-Forwarded-For`, who the client is. Defaults to loopback. Set it to your proxy if it is on another host, and never to a wildcard. |

## Setting up the X-408

The module talks to PitWatch and PitWatch never talks to the module, so all of
this is typed into the X-408's own web page, under **Setup**.

**General Settings**, then the **MQTT** tab at the bottom. Add a broker:

| Field | What to put |
| --- | --- |
| Hostname | The IP address of the machine running PitWatch |
| Port | `MQTT_PORT` from your `.env`, 1883 unless you moved it |
| Client ID | `x408` |
| Username, Password | `MQTT_USERNAME` and `MQTT_PASSWORD` from your `.env` |
| Encrypted | Off |
| Keep Alive | `30` |
| Clean Session | On |
| Birth Topic, Birth Message | `pitwatch/status`, `online` |
| Last Will Topic, Last Will Message | `pitwatch/status`, `offline` |

The birth and last will pair is what makes the module going offline something
PitWatch is told about rather than something it has to notice: the broker sends
the `offline` message on the module's behalf when the connection drops.

Then add a publication:

| Field | What to put |
| --- | --- |
| Publication Name | `inputs` |
| Broker | the one you just added |
| Publish on Change | **On** |
| Publish Interval | leave empty |
| Topic | `pitwatch/inputs` |
| QoS, Retain | `1`, On |

and for **Payload**, all eight inputs in one line:

```
{"1":${digitalInput1},"2":${digitalInput2},"3":${digitalInput3},"4":${digitalInput4},"5":${digitalInput5},"6":${digitalInput6},"7":${digitalInput7},"8":${digitalInput8}}
```

Every message carries all eight on purpose. One message per changed input would
leave PitWatch holding a picture assembled from fragments, and a fragment lost
across a reconnect would leave it quietly wrong.

The two topics also appear on the PitWatch settings page and have to match.
Nothing warns you if they do not, because a topic nobody publishes to looks
exactly like a module with nothing to say.

## Wiring the panel inputs

> Turn the panel off first, and if you are not comfortable working inside a pump
> control panel, have an electrician do it. PitWatch only reads; nothing here
> should change how the panel behaves, and if it does, something is wired wrong.

The X-408's inputs want **applied voltage**, 4 to 26 V DC. They do not supply
their own sensing current, so a free dry contact has to switch something. On a
24 V panel that is the control supply it is already sitting next to.

**The eight inputs share four negative terminals, a pair to each.** Inputs 1 and
2 return through one, 3 and 4 the next, and so on. Everything referencing one
control common is fine; it is worth knowing before you plan the wiring.

- **A signal the panel already energizes:** run the line to its input and the
  panel's control common to that input's negative terminal. Use the **panel's**
  common, not the X-408's own `Gnd`, or the isolation stops isolating anything.
- **A free dry contact:** put it in series between the control supply and its
  input, negative terminal on the control common.

Power it over **PoE** rather than from the panel. A module fed by the panel goes
quiet exactly when the panel loses power, which is one of the things you want to
be told about.

**Which way round is each input?** Use the live view on the settings page rather
than reasoning about it. Lift a float by hand and watch which row changes. Alarm
and overload contacts in particular are often held energized while healthy and
drop on the fault, so that a cut wire reads as a fault; those are the ones to
set to **on when voltage is missing**.

## Alerts

Each rule has four things you can set: whether it runs, how loudly (info,
warning or critical), whether it goes to administrators only, and what it says.
Everybody whose own level on the Users page is at or below the rule's level
hears it. The line you write is what a text says; an email sends the same line
and then the readings behind it.

Each is raised once and stays open until the condition goes away, so a float
that chatters twenty times sends one message and one all clear.

| Alert | Reads | Default |
| --- | --- | --- |
| High water | contacts | critical |
| Panel alert, unexplained | contacts | critical. Waits a few seconds to see whether something with detail explains it |
| Overload tripped | contacts | critical |
| Switched on, drawing nothing | both | critical. Neither sensor can see this alone |
| Drawing too much | clamps | off until you set the amps |
| Ran too long | contacts | off until you set the duration |
| Short cycling | clamps | warning. Usually a check valve letting the discharge run back |
| Nothing has run | clamps | warning. Either a dry spell or a blind monitor |
| Drawing more than it used to | clamps | info. The steady draw climbing week over week |
| A device stopped answering | PitWatch | warning, administrators only |
| Float activity | contacts | off. Every float, every time |
| A pump started | contacts | off. Every run |

Several cannot fire until the I/O module is wired, and the page says which.

## License

MIT.
