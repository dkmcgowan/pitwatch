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

    if (pump.drawing_current) {
      setFact(last, "running now");
    } else {
      setFact(last, recent.last_start ? since(recent.last_start) : null);
    }

    // One n/a on this row rather than three with punctuation between them. The
    // count and the dot are the same answer as the clock beside them, so they
    // go when it has nothing to say rather than each saying so themselves.
    setFact(runs, heard ? recent.runs + " today" : null);
    runs.hidden = !heard;
    const separator = card.querySelector("[data-runs-sep]");
    if (separator) {
      separator.hidden = !heard;
    }

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
    value.textContent = known ? "typical " + typical.median.toFixed(1) : "";
    value.hidden = !known;
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
      const wired = Boolean(lamp && lamp.channel);
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
        setFact(last, since(history.last_on));
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
  const DEVICE_NAMES = { shelly: "Shelly EM", inputs: "The X-408" };

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
