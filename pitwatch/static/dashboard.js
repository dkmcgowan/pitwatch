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

  function renderPump(number, pump) {
    const card = document.querySelector('[data-pump="' + number + '"]');
    if (!card || !pump) {
      return;
    }

    card.querySelector("[data-name]").textContent = pump.name || "Pump " + number;
    card.querySelector("[data-amps]").textContent = amps(pump.current);

    // Three states, not two. "No data" is its own answer and it is the one
    // that means go and look at why.
    const pill = card.querySelector("[data-run-pill]");
    if (pump.current === null) {
      setPill(pill, "No data", "idle");
      card.classList.remove("running");
    } else if (pump.running) {
      setPill(pill, "Running", "ok");
      card.classList.add("running");
    } else {
      setPill(pill, "Idle", "idle");
      card.classList.remove("running");
    }

    const nameplate = card.querySelector("[data-nameplate]");
    if (nameplate) {
      nameplate.textContent =
        pump.nameplate_amps === null ? "not set" : pump.nameplate_amps + " A";
    }

    renderTypical(card, pump.typical || {});
    renderFacts(number, pump);
  }

  // The three live facts under a run lamp. Amps first, because that is the one
  // that moves; then when it last started and how often today, which are the
  // two questions somebody standing in a wet basement actually asks.
  function renderFacts(number, pump) {
    const list = document.querySelector('[data-facts="' + number + '"]');
    if (!list) {
      return;
    }
    const recent = pump.recent || {};
    list.querySelector("[data-fact-amps]").textContent =
      pump.current === null ? "no data" : pump.current.toFixed(2) + " A";

    const last = list.querySelector("[data-fact-last]");
    if (pump.drawing_current) {
      last.textContent = "running now";
    } else if (recent.last_start) {
      last.textContent = since(recent.last_start);
    } else {
      last.textContent = "not in 24 h";
    }

    list.querySelector("[data-fact-runs]").textContent =
      recent.last_start || recent.runs ? String(recent.runs) : "--";
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

    if (typical.median === null || typical.median === undefined) {
      value.innerHTML = '<span class="muted">not enough runs yet</span>';
      drift.hidden = true;
      return;
    }
    value.textContent = typical.median.toFixed(1) + " A";

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
      const state = node.querySelector("[data-lamp-state]");
      const node_title = node.querySelector(".lamp-title");

      if (!lamp || !lamp.channel) {
        node.classList.remove("on");
        node.classList.add("unset");
        state.textContent = "not set";
        return;
      }
      node.classList.remove("unset");
      // The lamp keeps the name of the job, not the name of the wire. These
      // six are the six roles somebody assigned, and which input each one is
      // on is a question the settings page answers. Titles here also have to
      // fit a centered row, which "Pump 1 overload tripped" does not.
      if (node_title) {
        node_title.title = lamp.label ? lamp.label + " (DI" + lamp.channel + ")" : "";
      }
      if (lamp.state === null) {
        node.classList.remove("on");
        state.textContent = "no data";
        return;
      }
      node.classList.toggle("on", lamp.state === true);
      state.textContent = lamp.state ? "ON" : "off";
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

  function renderDevices(devices) {
    document.querySelectorAll("[data-device]").forEach(function (row) {
      const name = row.getAttribute("data-device");
      const device = (devices || {})[name];
      const pill = row.querySelector("[data-device-pill]");
      const detail = row.querySelector("[data-device-detail]");

      if (!device) {
        setPill(pill, "Unknown", "idle");
        detail.textContent = "";
        return;
      }
      // Deliberately not set up is not a fault. Starting with only the clamps
      // wired is a normal way to run, and painting that red would train
      // somebody to ignore the one place that goes red when it matters.
      if (!device.configured) {
        setPill(pill, "Not set up", "idle");
        detail.textContent = "";
        return;
      }
      if (device.online) {
        setPill(pill, "Connected", "ok");
        detail.textContent = "";
      } else {
        setPill(pill, "Offline", "crit");
        detail.textContent =
          (device.last_error || "not reachable") +
          (device.last_seen ? ", last heard from " + since(device.last_seen) : "");
      }
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
    renderInputs(state.inputs);
    renderDevices(state.devices);
    renderBanner(state);
    document.body.classList.remove("stale");
  }

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
