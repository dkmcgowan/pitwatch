// The dashboard.
//
// One renderer, fed first by a fetch so the page has data immediately and then
// by a websocket so it stays current. Both carry the same payload, so there is
// no second code path that can drift from the first.
//
// The rule this file follows throughout: null is not false. A float that is not
// wired to any channel reads as "not connected", never as "dry". A dashboard
// that shows a high water float as safe when nothing is reporting it is worse
// than one that admits it does not know.

(function () {
  "use strict";

  const RECONNECT_MIN_MS = 1000;
  const RECONNECT_MAX_MS = 30000;
  let reconnectDelay = RECONNECT_MIN_MS;
  let socket = null;

  // -- helpers --------------------------------------------------------------

  // Null rather than a dash of its own when there is no reading. What to say
  // in that case is setFact's to decide, and it is the only thing that decides
  // it: this used to answer "--", which put a monospaced dash at the top of a
  // column of n/a on every install that had not heard from a meter yet.
  function amps(value) {
    return typeof value === "number" ? value.toFixed(2) : null;
  }

  function since(iso) {
    if (!iso) {
      return "";
    }
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) {
      return "";
    }
    const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (seconds < 60) {
      return seconds + "s ago";
    }
    if (seconds < 3600) {
      return Math.round(seconds / 60) + " min ago";
    }
    if (seconds < 86400) {
      return Math.round(seconds / 3600) + " h ago";
    }
    return Math.round(seconds / 86400) + " d ago";
  }

  // How long a run lasted, in the units somebody would say it in. A pit that
  // runs for seconds at a time deserves seconds; a run into the minutes is one
  // worth noticing, and the whole point of showing this is that a run getting
  // longer is a pump getting tired.
  function duration(seconds) {
    if (typeof seconds !== "number" || seconds < 1) {
      return null;
    }
    if (seconds < 90) {
      return Math.round(seconds) + " s";
    }
    if (seconds < 5400) {
      return Math.round(seconds / 60) + " min";
    }
    return (seconds / 3600).toFixed(1) + " h";
  }

  // -- rendering ------------------------------------------------------------

  // One way of saying there is nothing to say, so four fields cannot drift
  // into four different ways of saying it. Anything absent reads n/a and reads
  // dimmer, which is what tells a card with no data behind it from a working
  // one at a glance.
  function setFact(node, value) {
    if (!node) {
      return;
    }
    const missing = value === null || value === undefined || value === "";
    node.textContent = missing ? "n/a" : value;
    node.classList.toggle("none", missing);
  }

  function renderPump(number, pump) {
    const card = document.querySelector('[data-pump="' + number + '"]');
    if (!card || !pump) {
      return;
    }

    card.querySelector("[data-name]").textContent = pump.name || "Pump " + number;

    // Load now reads the same way as the three rows under it when there is
    // nothing behind it. The unit goes with the number: "n/a A" is not a
    // reading, and the A on its own is a label for something that is not
    // there.
    const reading = amps(pump.current);
    setFact(card.querySelector("[data-amps]"), reading);
    card.querySelector(".unit").hidden = reading === null;

    // The whole section says it while a pump is running: outlined and tinted,
    // with its icon and its amps green. There is no separate lamp beside the
    // name any more, and no pill either. Both were a small shape repeating
    // what the section it sits in already says.
    card.classList.toggle("running", pump.running === true);

    renderTypical(card, pump.typical || {});
    renderRecent(card, pump);
  }

  // When it last started, how many times it has started today, and how many
  // times it starts on an ordinary day. Three answers to one question, so they
  // are one row rather than three: asking it three times down a card is what
  // made the pump the tallest thing on this page.
  function renderRecent(card, pump) {
    const last = card.querySelector("[data-fact-last]");
    const runs = card.querySelector("[data-fact-runs]");
    if (!last || !runs) {
      return;
    }
    const recent = pump.recent || {};

    // Nothing to count and nothing that ever ran are the same answer here. A
    // clamp that has never seen a run and a clamp that is not fitted look
    // identical from this side, so neither gets to claim a confident zero.
    const heard = Boolean(recent.last_start || recent.runs || pump.drawing_current);

    if (pump.running) {
      setFact(last, "running now");
    } else if (recent.last_start) {
      // How long it ran, beside when it ran. The duration is the answer to
      // "is this pump working harder than it was", and it only exists where
      // the panel's own run contact is wired: a clamp cannot say when a run
      // ended to better than its own reporting interval.
      const lasted = duration(recent.last_duration_s);
      setFact(last, lasted ? since(recent.last_start) + " for " + lasted : since(recent.last_start));
    } else {
      setFact(last, null);
    }

    renderDurationDrift(card, recent);

    // The second line, which says n/a when there is nothing rather than
    // disappearing. Every other second line on this board does the same, and a
    // row that is one line tall next to one that is two stops the sections
    // lining up with each other.
    setFact(runs, heard ? recent.runs + " today" : null);

    // An ordinary day beside today's count. Eighty-nine is a lot or a Tuesday
    // depending on what the month looks like, and only one of those is worth
    // getting out of bed for.
    const average = card.querySelector("[data-fact-average]");
    if (average) {
      const known =
        recent.daily_average !== null && recent.daily_average !== undefined && heard;
      average.textContent = known ? "avg " + recent.daily_average : "";
      average.hidden = !known;
    }
  }

  // Whether a run is taking longer than it used to.
  //
  // The duration's half of the question the typical load answers about amps,
  // and on this hardware the better half: a duration comes from the contacts
  // and is exact, where amps are whatever the meter happened to report. A pump
  // taking longer to shift the same pit is one losing capacity, and no single
  // run shows it.
  //
  // Shown only when it has moved, so it costs nothing on a pump that is fine,
  // and what it moved against is in the tooltip rather than on the line.
  function renderDurationDrift(card, recent) {
    const drift = card.querySelector("[data-duration-drift]");
    if (!drift) {
      return;
    }
    const moved = recent.duration_drift_s;
    const usual = recent.typical_duration_s;
    const known = typeof moved === "number" && typeof usual === "number";

    if (known) {
      const up = moved > 0;
      drift.className = up ? "beside drift-up" : "beside";
      drift.textContent = (up ? "up " : "down ") + duration(Math.abs(moved));
      drift.title =
        "Runs last " +
        duration(usual) +
        " this week, against " +
        duration(usual - moved) +
        " over the weeks before.";
    }
    drift.hidden = !known;
  }

  // What the pump draws when it is actually running, and whether that is
  // moving. The second part is the point: a steady draw climbing over weeks is
  // an impeller packing up or a bearing going dry, and it is invisible in any
  // single reading.
  //
  // Beside the live reading rather than on a line of its own. They are the same
  // measurement at two moments, and the live one is zero nearly every time
  // anybody looks: giving zero half the card and putting the number worth
  // watching underneath it had the sizing exactly backwards.
  //
  // It goes rather than saying n/a when there is nothing behind it. The row
  // already has an answer, which is the amps; a second n/a on the same line
  // says the same nothing twice.
  function renderTypical(card, typical) {
    const value = card.querySelector("[data-typical]");
    const drift = card.querySelector("[data-drift]");
    if (!value || !drift) {
      return;
    }

    const known = typical.median !== null && typical.median !== undefined;
    // With its unit. Every other amp reading on this card carries one, and a
    // bare "typical 15.4" beside "0.00 A" reads as a different kind of number
    // rather than the same measurement at a different moment.
    // The second line of the load row, so it answers n/a when there is nothing
    // rather than vanishing. It hid itself while it shared a line with the
    // amps, where a second n/a would have said the same nothing twice.
    setFact(value, known ? "typical " + typical.median.toFixed(1) + " A" : null);
    value.title = known
      ? "The middle reading while the pump was running this week, less the starting surge."
      : "";

    // Nothing to compare, or a tenth of an amp either way, which is
    // measurement rather than a trend.
    const moving =
      known &&
      typical.drift !== null &&
      typical.drift !== undefined &&
      Math.abs(typical.drift) >= 0.2;
    if (!moving) {
      drift.hidden = true;
      drift.textContent = "";
      return;
    }

    const up = typical.drift > 0;
    drift.className = up ? "beside drift-up" : "beside";
    drift.textContent = (up ? "up " : "down ") + Math.abs(typical.drift).toFixed(1);
    drift.title =
      "Against " + typical.earlier_median.toFixed(1) + " A over the four weeks before.";
    drift.hidden = false;
  }

  // The panel door.
  //
  // Three states per lamp and they are all different: on, off, and nothing to
  // say. A lamp with no input assigned and a lamp whose input has never been
  // read both read "not set" and stay dark rather than reading off, because a
  // dark lamp that means unknown is the lamp somebody trusts.

  function renderPanel(panel) {
    const lamps = panel || {};

    document.querySelectorAll("[data-lamp]").forEach(function (node) {
      const role = node.getAttribute("data-lamp");
      const lamp = lamps[role];
      const node_title = node.querySelector(".lamp-title");

      // A lamp says one thing: lit or not. It used to carry a line of text
      // under it saying "not set" or "no data", which is a sentence where an
      // indicator should be. What it has been doing is at the end of the same
      // row, in numbers.
      //
      // An unassigned lamp still draws dimmer, which is the one piece of that
      // distinction worth keeping without words: dark because nobody wired it
      // reads differently from dark because the contact is open.
      // Watched, rather than wired: one row on this board is not an input at
      // all but two of them read together, and it is watched exactly when both
      // of those have been assigned.
      const wired = Boolean(lamp && (lamp.channel || lamp.watched));
      node.classList.toggle("unset", !wired);
      node.classList.toggle("on", wired && lamp.state === true);

      if (node_title) {
        node_title.title = wired
          ? (lamp.label || lamp.title) + " on DI" + lamp.channel
          : "No input assigned";
      }
    });

    renderAlertSummary(lamps);

    const display = lamps.display || { 1: "--", 2: "--" };
    renderStatus(1, display["1"]);
    renderStatus(2, display["2"]);
  }

  // The four alert rows added up, beside the heading.
  //
  // Four dark bulbs already say nothing is up. They say it by being four
  // things somebody has to read and find dark, which is a page that has to be
  // checked rather than one that reports, and the whole reason anybody opens
  // this on a phone is to be told.
  //
  // It counts what it can see and says so no harder than that. An alert with
  // no input assigned is not being watched, and rolling it into "all clear"
  // would be the page claiming something nobody wired. That case is in the
  // tooltip, and the row itself is drawn dim.
  const ALERT_ROLES = ["system_alert", "high_water", "pump1_fault", "pump2_fault"];

  function renderAlertSummary(lamps) {
    const badge = document.querySelector("[data-alert-summary]");
    if (!badge) {
      return;
    }

    const watched = ALERT_ROLES.map(function (role) {
      return lamps[role];
    }).filter(function (lamp) {
      return Boolean(lamp && lamp.channel);
    });
    const lit = watched.filter(function (lamp) {
      return lamp.state === true;
    });

    // Nothing assigned at all, which is a fresh install rather than a quiet
    // one. The same dash the pumps show when the panel has not spoken.
    if (!watched.length) {
      badge.className = "status status-none";
      badge.textContent = "--";
      badge.title = "No alert inputs are assigned yet";
      return;
    }

    if (lit.length) {
      badge.className = "status status-alarm";
      badge.textContent = lit.length + " active";
      badge.title = lit
        .map(function (lamp) {
          return lamp.label || lamp.title;
        })
        .join(", ");
      return;
    }

    badge.className = "status status-clear";
    badge.textContent = "All clear";
    badge.title =
      watched.length === ALERT_ROLES.length
        ? "Nothing raised on any of the four"
        : "Nothing raised on the " + watched.length + " assigned. The rest have no input.";
  }

  // The controller's word for a pump: LEAD, LAG, ON or FAIL, beside the name
  // it is about.
  //
  // It was a green screen across the middle of the page reading "P1:LEAD
  // P2:LAG", drawn to look like the display on the panel door. That was a
  // picture of a display rather than a display: a band of the page spent on
  // two words, with neither word anywhere near the pump it described.
  //
  // The words are the panel's, not ours. Somebody who has stood in front of
  // that controller already knows how to read them, and the note behind the i
  // says what they mean for somebody who has not.
  const MEANS = {
    LEAD: "Answers the next call, and running while it runs",
    LAG: "Sitting this one out",
    ON: "Running: the controller has called both pumps",
    FAIL: "Overload tripped. This pump is off and staying off"
  };

  function renderStatus(number, word) {
    const card = document.querySelector('[data-pump="' + number + '"]');
    const badge = card && card.querySelector("[data-status]");
    if (!badge) {
      return;
    }
    // Nothing has run since this was wired up, so the controller has not said
    // which pump is lead and neither do we. A dash is the honest answer; a
    // guess would be wrong half the time.
    const known = Boolean(MEANS[word]);
    badge.textContent = known ? word : "--";
    badge.className = "status " + (known ? "status-" + word.toLowerCase() : "status-none");
    badge.title = known
      ? MEANS[word]
      : "Waiting for the panel to say which pump is lead";
  }

  // What each contact has been doing, on the lamp's own row. Same panel
  // payload the lamps read, so a lamp and the lines beside it can never
  // disagree.
  //
  // Each count carries the window it counted, because they are not the same
  // window and a bare number would read as one. A float closes every time the
  // pit fills, so a day is the useful figure; an alarm counted by the day
  // reads zero forever and teaches somebody to stop looking.
  const COUNTED = { today: "today", month: "this month" };

  function renderHistory(panel) {
    const lamps = panel || {};

    document.querySelectorAll("[data-history]").forEach(function (row) {
      const lamp = lamps[row.getAttribute("data-history")];
      const last = row.querySelector("[data-history-last]");
      const count = row.querySelector("[data-history-count]");
      const group = row.closest("[data-window]");
      const window_ = (group && group.getAttribute("data-window")) || "today";
      const history = (lamp && lamp.history) || {};

      // Zero is a real answer here, unlike a run count from a clamp that might
      // not be fitted: an input somebody has assigned and PitWatch has read is
      // an input whose quiet month means something.
      const times = history[window_];
      const counted = times !== null && times !== undefined;

      if (history.last_on) {
        // How long it stayed closed, beside when it closed. A float that is
        // wet for sixteen seconds and one wet for six minutes are the same
        // row without it, and they are not the same news.
        const held = duration(history.last_held_s);
        setFact(last, held ? since(history.last_on) + " for " + held : since(history.last_on));
      } else {
        // Never is an answer; n/a is the absence of one. A contact that has
        // been read all month and has not closed says never. One nobody has
        // wired has nothing to say either way.
        setFact(last, counted ? "never" : null);
      }
      // Both lines, always, whether or not there is anything behind them. A
      // lamp with one line beside it is a row shorter than the one under it,
      // and with some inputs wired and some not the sections stop lining up
      // with each other.
      setFact(count, counted ? times + " " + (COUNTED[window_] || window_) : null);

      // An ordinary day beside today's count, on the sections counted by the
      // day. Not on the ones counted by the month: an alarm's average is a
      // decimal nobody can act on, and "2 this month" is already the whole
      // story.
      const average = row.querySelector("[data-history-average]");
      if (average) {
        const known =
          window_ === "today" &&
          history.daily_average !== null &&
          history.daily_average !== undefined;
        average.textContent = known ? "avg " + history.daily_average : "";
        average.hidden = !known;
      }
    });
  }

  // One indicator per device, named.
  //
  // "Something is offline" and "the meter is offline" are different amounts of
  // use to somebody standing in a basement, and the two fail for entirely
  // different reasons: the Shelly drops off wifi, the X-408 stops being able
  // to reach the broker.
  //
  // Three states rather than two. Deliberately not set up is not a fault, and
  // it is the reason a device that is off is hollow rather than red: running
  // on the clamps alone is a normal way to run, and a permanent red for it
  // would teach whoever reads this page that red means nothing.
  // Named after the job rather than the hardware, because the hardware behind
  // a source is a setting now. A name that is not here falls back to the role,
  // which is already a word rather than an id.
  // Singular, because these are read into "X is connected". "The panel
  // contacts is connected" is what naming them after the role rather than the
  // thing gets you, and the sentence is what somebody actually reads.
  const DEVICE_NAMES = {
    health0: "The inputs",
    health1: "The meter",
    clamp1: "The meter",
    clamp2: "The second meter",
  };

  function renderLinks(devices) {
    const known = devices || {};

    document.querySelectorAll("[data-link]").forEach(function (box) {
      const name = box.getAttribute("data-link");
      const device = known[name];
      const dot = box.querySelector(".link-dot");
      const said = box.querySelector("[data-link-said]");
      const label = DEVICE_NAMES[name] || name;

      let state = "idle";
      let words = label + " is not set up";

      if (device && device.configured) {
        if (device.online) {
          state = "ok";
          words = label + " is connected";
        } else {
          state = "crit";
          words =
            label +
            " is offline: " +
            (device.last_error || "not reachable") +
            (device.last_seen ? ", last heard from " + since(device.last_seen) : "");
        }
      }

      dot.className = "link-dot link-" + state;
      box.classList.toggle("link-off", state === "idle");
      // A title is a tooltip, which a phone cannot hover over and a screen
      // reader may or may not read. The hidden span is the same words where
      // they will always be found.
      box.title = words;
      said.textContent = words;
    });
  }

  // -- rain -----------------------------------------------------------------
  //
  // The only card on this page that looks forward. Drawn as SVG rather than as
  // divs for the same reason the history charts are: the content security
  // policy allows no inline styles, so a bar whose height is set from script
  // has to carry it as an attribute, and SVG geometry is attributes.

  const RAIN_NS = "http://www.w3.org/2000/svg";

  function rainSvg(name, attrs) {
    const node = document.createElementNS(RAIN_NS, name);
    Object.keys(attrs).forEach(function (key) {
      node.setAttribute(key, attrs[key]);
    });
    return node;
  }

  function rainAmount(value, units) {
    if (value === null || value === undefined) {
      return "--";
    }
    return units === "mm" ? value.toFixed(1) + " mm" : value.toFixed(2) + '"';
  }

  // A ceiling that keeps a light shower from filling the strip. Without a
  // floor, two hundredths of an inch draws the same wall of bars as an inch
  // does, and the card would cry wolf every time it drizzled.
  function rainTop(values, units) {
    const floor = units === "mm" ? 2 : 0.08;
    const highest = values.reduce(function (top, value) {
      return Math.max(top, value || 0);
    }, 0);
    return Math.max(floor, highest);
  }

  function drawRainStrip(holder, rain) {
    holder.textContent = "";
    const hours = rain.hours || [];
    if (!hours.length) {
      return;
    }

    const width = Math.max(200, Math.round(holder.clientWidth));
    const height = 54;
    const canvas = rainSvg("svg", {
      width: width,
      height: height,
      role: "img",
      "aria-label": "Rain by the hour, a day behind and a day ahead",
    });

    const top = rainTop(
      hours.map(function (hour) {
        return hour[1];
      }),
      rain.units
    );
    const slot = width / hours.length;
    const bar = Math.max(1, slot - 1);
    const floor = height - 10;

    hours.forEach(function (hour, index) {
      const value = hour[1] || 0;
      const ahead = hour[2];
      const tall = Math.max(value > 0 ? 1.5 : 0, (floor * Math.min(1, value / top)));
      if (tall > 0) {
        canvas.appendChild(
          rainSvg("rect", {
            x: (index * slot).toFixed(1),
            y: (floor - tall).toFixed(1),
            width: bar.toFixed(1),
            height: tall.toFixed(1),
            // What is coming is drawn lighter than what fell. A forecast and
            // a measurement are different kinds of claim and should not look
            // identical on a page somebody acts on.
            //
            // Colored by class rather than by a fill attribute, so the palette
            // stays in the stylesheet with the rest of it. A class attribute
            // is not an inline style, so the content security policy is
            // content.
            class: ahead ? "rain-bar rain-bar-ahead" : "rain-bar",
            rx: 1,
          })
        );
      }
    });

    // Now. The one thing that makes the strip readable: without it there is no
    // telling which half already happened.
    const split = hours.findIndex(function (hour) {
      return hour[2];
    });
    if (split > 0) {
      const x = (split * slot).toFixed(1);
      canvas.appendChild(
        rainSvg("line", {
          x1: x,
          x2: x,
          y1: 0,
          y2: floor,
          class: "rain-split",
        })
      );
    }

    canvas.appendChild(
      rainSvg("line", {
        x1: 0,
        x2: width,
        y1: floor,
        y2: floor,
        class: "rain-floor",
      })
    );

    ["24h ago", "now", "+24h"].forEach(function (word, index) {
      const label = rainSvg("text", {
        x: index === 0 ? 0 : index === 1 ? width / 2 : width,
        y: height - 1,
        "text-anchor": index === 0 ? "start" : index === 1 ? "middle" : "end",
        class: "chart-label",
      });
      label.textContent = word;
      canvas.appendChild(label);
    });

    holder.appendChild(canvas);
  }

  function renderRain(rain) {
    const card = document.querySelector("[data-rain]");
    if (!card) {
      return;
    }
    const empty = card.querySelector("[data-rain-empty]");
    const strip = card.querySelector("[data-rain-strip]");
    const state = card.querySelector("[data-rain-state]");
    const fell = card.querySelector("[data-rain-fell]");
    const coming = card.querySelector("[data-rain-coming]");
    const scale = card.querySelector("[data-rain-scale]");
    const age = card.querySelector("[data-rain-age]");

    // Nothing stored is a different answer from no rain, and the card has to
    // say which. One means the pit is dry; the other means nobody has looked.
    const known = Boolean(rain);
    if (empty) {
      empty.hidden = known;
    }
    [strip, state, fell, coming].forEach(function (node) {
      if (node && node.parentElement) {
        node.parentElement.hidden = !known;
      }
    });
    if (!known) {
      return;
    }

    if (state) {
      if (rain.now && rain.frozen) {
        // Frozen precipitation is not in the pit yet and saying "raining"
        // would be the one wrong word on the card.
        state.textContent = (rain.doing || "snow") + " now, which reaches the pit when it melts";
        state.className = "rain-state rain-on";
      } else if (rain.now) {
        state.textContent = (rain.doing || "raining") + " now";
        state.className = "rain-state rain-on";
      } else if (rain.chance !== null && rain.chance !== undefined && rain.chance >= 30) {
        state.textContent = "dry now, " + rain.chance + "% chance in the next day";
        state.className = "rain-state";
      } else {
        state.textContent = "dry";
        state.className = "rain-state";
      }
    }

    if (fell) {
      fell.textContent = rainAmount(rain.last_24h, rain.units);
    }
    if (coming) {
      coming.textContent = rainAmount(rain.next_24h, rain.units);
    }
    if (strip) {
      drawRainStrip(strip, rain);
    }
    if (scale) {
      const hours = rain.hours || [];
      const top = rainTop(
        hours.map(function (hour) {
          return hour[1];
        }),
        rain.units
      );
      scale.textContent = "tallest bar " + rainAmount(top, rain.units) + " in an hour";
    }
    if (age) {
      // A forecast that stopped refreshing is one to distrust, and the only
      // way anybody can tell is if the card says when it last did.
      age.textContent = rain.fetched_at ? "checked " + since(rain.fetched_at) : "";
    }
  }

  function renderBanner(state) {
    const banner = document.querySelector("[data-banner]");
    if (!banner) {
      return;
    }

    // The one thing worth interrupting the page for. Everything else has its
    // own place on the layout.
    //
    const panel = state.panel || {};
    const high = panel.high_water;
    const both = state.pumps && state.pumps["1"].running && state.pumps["2"].running;

    if (high && high.state) {
      banner.className = "banner banner-crit";
      banner.textContent = "High water. Both pumps should be running.";
      banner.hidden = false;
    } else if (both) {
      banner.className = "banner banner-warn";
      banner.textContent = "Both pumps are running.";
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }
  }

  function render(state) {
    renderPump(1, (state.pumps || {})["1"]);
    renderPump(2, (state.pumps || {})["2"]);
    renderPanel(state.panel);
    renderHistory(state.panel);
    renderLinks(state.devices);
    renderRain(state.rain);
    renderBanner(state);
    document.body.classList.remove("stale");
  }

  // -- transport ------------------------------------------------------------

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(scheme + "//" + window.location.host + "/ws/state");

    socket.addEventListener("message", function (event) {
      // Only the parse is forgiven. A malformed frame is not a reason to tear
      // down a working socket, but a mistake in the renderer is not a
      // malformed frame, and wrapping both in one catch is how a missing
      // function went unnoticed through two releases: every frame threw, every
      // throw was swallowed, and the page sat there saying it was not
      // connected. Let that one reach the console.
      let state;
      try {
        state = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      render(state);
      reconnectDelay = RECONNECT_MIN_MS;
    });

    socket.addEventListener("close", function () {
      socket = null;
      // Marks the whole page as stale rather than leaving numbers on screen
      // that look live and are not. Losing the feed is itself information.
      document.body.classList.add("stale");
      window.setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
    });

    socket.addEventListener("error", function () {
      if (socket) {
        socket.close();
      }
    });
  }

  async function first() {
    let state;
    try {
      const response = await fetch("/api/state");
      if (!response.ok) {
        return;
      }
      state = await response.json();
    } catch (error) {
      // The websocket is about to try anyway.
      return;
    }
    render(state);
  }

  first();
  connect();
})();
