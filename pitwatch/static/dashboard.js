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

  function setPill(element, text, kind) {
    if (!element) {
      return;
    }
    element.textContent = text;
    element.className = "pill pill-" + kind;
  }

  function amps(value) {
    return typeof value === "number" ? value.toFixed(2) : "--";
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
    card.querySelector("[data-amps]").textContent = amps(pump.current);

    // The card outlines itself while a pump is running. There is no pill any
    // more: the amps above say the same thing in a number somebody wanted
    // anyway, and two things saying it is two things to read.
    card.classList.toggle("running", pump.running === true);

    const lamp = card.querySelector("[data-pump-lamp]");
    if (lamp) {
      lamp.classList.toggle("on", pump.running === true);
      lamp.title = pump.running ? "Running" : "Not running";
    }

    setFact(
      card.querySelector("[data-nameplate]"),
      pump.nameplate_amps === null ? null : pump.nameplate_amps + " A"
    );

    renderTypical(card, pump.typical || {});
    renderRecent(card, pump);
  }

  // When it last started and how often today, which are the two questions
  // somebody standing in a wet basement actually asks. On the pump's own card,
  // with the amps, because they are all facts about the same motor.
  function renderRecent(card, pump) {
    const last = card.querySelector("[data-fact-last]");
    const runs = card.querySelector("[data-fact-runs]");
    if (!last || !runs) {
      return;
    }
    const recent = pump.recent || {};

    if (pump.drawing_current) {
      setFact(last, "running now");
    } else {
      setFact(last, recent.last_start ? since(recent.last_start) : null);
    }

    // Nothing to count and nothing that ever ran are the same answer here. A
    // clamp that has never seen a run and a clamp that is not fitted look
    // identical from this side, so neither gets to claim a confident zero.
    setFact(runs, recent.last_start || recent.runs ? String(recent.runs) : null);

    // An ordinary day beside today's count. Eighty-nine is a lot or a Tuesday
    // depending on what the month looks like, and only one of those is worth
    // getting out of bed for.
    const average = card.querySelector("[data-fact-average]");
    if (average) {
      const known =
        recent.daily_average !== null &&
        recent.daily_average !== undefined &&
        (recent.last_start || recent.runs);
      average.textContent = known ? "avg " + recent.daily_average : "";
      average.hidden = !known;
    }
  }

  // What the pump draws when it is actually running, and whether that is
  // moving. The second part is the point: a steady draw climbing over weeks is
  // an impeller packing up or a bearing going dry, and it is invisible in any
  // single reading.
  function renderTypical(card, typical) {
    const value = card.querySelector("[data-typical]");
    const drift = card.querySelector("[data-drift]");
    if (!value || !drift) {
      return;
    }

    // n/a rather than a sentence explaining itself. There is no room for a
    // sentence in a two by two grid, and the settings page is where the reason
    // belongs.
    const known = typical.median !== null && typical.median !== undefined;
    setFact(value, known ? typical.median.toFixed(1) + " A" : null);
    if (!known) {
      drift.hidden = true;
      return;
    }

    if (typical.drift === null || typical.drift === undefined) {
      drift.hidden = true;
      return;
    }
    // A tenth of an amp either way is measurement, not a trend.
    if (Math.abs(typical.drift) < 0.2) {
      drift.className = "drift";
      drift.textContent = "Steady against the four weeks before.";
      drift.hidden = false;
      return;
    }
    const up = typical.drift > 0;
    drift.className = up ? "drift drift-up" : "drift";
    drift.textContent =
      (up ? "Up " : "Down ") +
      Math.abs(typical.drift).toFixed(1) +
      " A on the four weeks before, which were " +
      typical.earlier_median.toFixed(1) +
      " A.";
    drift.hidden = false;
  }

  // Which inputs exist and what they are called are settings, so the rows are
  // built from what arrives rather than written into the page. Rebuilt only
  // when the set of inputs actually changes; every other update writes into
  // the rows already there, because replacing them once a second would throw
  // away the selection of anyone reading one.
  let builtFrom = null;

  function buildInputs(container, list) {
    container.textContent = "";
    list.forEach(function (reading) {
      const row = document.createElement("div");
      row.className = "float";
      row.setAttribute("data-input", reading.channel);
      // Built rather than assigned as HTML: the name is whatever somebody
      // typed on the settings page, and it goes in as text.
      const name = document.createElement("span");
      name.className = "float-name";
      const pill = document.createElement("span");
      pill.className = "pill pill-idle";
      pill.setAttribute("data-input-pill", "");
      const stamp = document.createElement("span");
      stamp.className = "float-since muted";
      stamp.setAttribute("data-input-since", "");
      row.appendChild(name);
      row.appendChild(pill);
      row.appendChild(stamp);
      container.appendChild(row);
    });
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
      // indicator should be. What it has been doing is answered downstairs.
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

    const lcd = document.querySelector("[data-lcd]");
    if (lcd) {
      const display = lamps.display || { 1: "--", 2: "--" };
      lcd.textContent = "P1:" + display["1"] + "  P2:" + display["2"];
      lcd.classList.toggle(
        "lcd-fail",
        display["1"] === "FAIL" || display["2"] === "FAIL"
      );
    }

    // The overloads have no lamp, so say in words when one has tripped.
    const note = document.querySelector("[data-door-note]");
    if (note) {
      const tripped = ["pump1_fault", "pump2_fault"].filter(function (role) {
        return lamps[role] && lamps[role].state === true;
      });
      if (tripped.length) {
        note.textContent =
          tripped.length === 2
            ? "Both overloads have tripped. Neither pump can start."
            : "The " +
              (lamps[tripped[0]].label || tripped[0]) +
              " overload has tripped. That pump cannot start.";
        note.hidden = false;
      } else {
        note.hidden = true;
      }
    }
  }

  // What each contact has been doing. One row per lamp, reading the same
  // panel payload the lamps do, so a row and its lamp can never disagree.
  function renderHistory(panel) {
    const lamps = panel || {};

    document.querySelectorAll("[data-history]").forEach(function (row) {
      const lamp = lamps[row.getAttribute("data-history")];
      const last = row.querySelector("[data-history-last]");
      const count = row.querySelector("[data-history-count]");
      const window_ = row.closest("table").getAttribute("data-window") || "today";
      const history = (lamp && lamp.history) || {};

      setFact(last, history.last_on ? since(history.last_on) : null);
      // Zero is a real answer here, unlike a run count from a clamp that might
      // not be fitted: an input somebody has assigned and PitWatch has read is
      // an input whose quiet month means something.
      const times = history[window_];
      setFact(count, times === null || times === undefined ? null : String(times));
    });
  }

  function renderInputs(inputs) {
    const container = document.querySelector("[data-inputs]");
    if (!container) {
      return;
    }
    const list = inputs || [];
    const card = document.querySelector("[data-other-card]");
    if (card) {
      card.hidden = list.length === 0;
    }
    const shape = list
      .map(function (reading) {
        return reading.channel + ":" + reading.label;
      })
      .join("|");
    if (shape !== builtFrom) {
      builtFrom = shape;
      buildInputs(container, list);
    }

    list.forEach(function (reading, index) {
      const row = container.children[index];
      if (!row) {
        return;
      }
      const pill = row.querySelector("[data-input-pill]");
      const stamp = row.querySelector("[data-input-since]");
      row.querySelector(".float-name").textContent = reading.label;

      if (reading.state === null) {
        setPill(pill, "No data", "idle");
        stamp.textContent = "";
        row.classList.remove("wet");
        return;
      }
      if (reading.state) {
        setPill(pill, reading.on_word, "warn");
        row.classList.add("wet");
      } else {
        setPill(pill, reading.off_word, "ok");
        row.classList.remove("wet");
      }
      stamp.textContent = since(reading.changed_at);
    });
  }

  // The two devices as one indicator.
  //
  // Named after what it is telling you rather than after what it is made of:
  // whether the monitoring is connected. The words that used to be in two rows
  // of pills are still here, in the tooltip, for whoever wants them.
  //
  // Deliberately not set up is not a fault. Starting with only the clamps
  // wired is a normal way to run, and painting that red would train somebody
  // to ignore the one thing on this page that goes red when it matters.
  const DEVICE_NAMES = { shelly: "Shelly EM", inputs: "Panel inputs" };

  function renderLink(devices) {
    const box = document.querySelector("[data-link]");
    if (!box) {
      return;
    }
    const dot = box.querySelector(".link-dot");
    const word = box.querySelector("[data-link-word]");
    const said = box.querySelector("[data-link-said]");

    const known = devices || {};
    const configured = Object.keys(DEVICE_NAMES).filter(function (name) {
      return known[name] && known[name].configured;
    });
    const down = configured.filter(function (name) {
      return !known[name].online;
    });

    const detail = Object.keys(DEVICE_NAMES)
      .map(function (name) {
        const device = known[name];
        if (!device || !device.configured) {
          return DEVICE_NAMES[name] + ": not set up";
        }
        if (device.online) {
          return DEVICE_NAMES[name] + ": connected";
        }
        return (
          DEVICE_NAMES[name] +
          ": offline, " +
          (device.last_error || "not reachable") +
          (device.last_seen ? ", last heard from " + since(device.last_seen) : "")
        );
      })
      .join(". ");

    let state = "ok";
    let label = "";
    if (!configured.length) {
      state = "idle";
    } else if (down.length === 1) {
      state = "crit";
      label = DEVICE_NAMES[down[0]] + " offline";
    } else if (down.length > 1) {
      state = "crit";
      label = "Both devices offline";
    }

    dot.className = "link-dot link-" + state;
    word.textContent = label;
    box.title = detail;
    // The tooltip is a title attribute, which a screen reader may or may not
    // announce and a phone cannot hover over at all. This is the same words
    // where they will always be read.
    said.textContent = detail;
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
    renderInputs(state.inputs);
    renderLink(state.devices);
    renderBanner(state);
    document.body.classList.remove("stale");
  }

  // -- the information notes ------------------------------------------------
  //
  // A native dialog, opened as a modal. It renders in the top layer, so it
  // cannot push a card around or end up behind one whatever the stacking looks
  // like, and Escape closes it without being told to. Clicking anywhere closes
  // it, including inside: there is nothing in there to interact with, and a
  // note that needs a target found before it will go away is a note that gets
  // left open.
  //
  // Only ever one at a time, because that is what showModal means.

  function wireNotes() {
    document.querySelectorAll("[data-info]").forEach(function (button) {
      const note = document.getElementById("note-" + button.getAttribute("data-info"));
      if (!note || typeof note.showModal !== "function") {
        // No dialog support: leave the button doing nothing rather than
        // opening something that cannot be closed.
        button.hidden = true;
        return;
      }
      button.addEventListener("click", function () {
        note.showModal();
      });
      note.addEventListener("click", function () {
        note.close();
      });
    });
  }

  wireNotes();

  // -- transport ------------------------------------------------------------

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(scheme + "//" + window.location.host + "/ws/state");

    socket.addEventListener("message", function (event) {
      try {
        render(JSON.parse(event.data));
        reconnectDelay = RECONNECT_MIN_MS;
      } catch (error) {
        // A malformed frame is not a reason to tear down a working socket.
      }
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
    try {
      const response = await fetch("/api/state");
      if (response.ok) {
        render(await response.json());
      }
    } catch (error) {
      // The websocket is about to try anyway.
    }
  }

  first();
  connect();
})();
