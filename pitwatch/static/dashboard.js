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
    if (pump.current === null && pump.run_contact === null) {
      setPill(pill, "No data", "idle");
      card.classList.remove("running");
    } else if (pump.running) {
      setPill(pill, "Running", "ok");
      card.classList.add("running");
    } else {
      setPill(pill, "Idle", "idle");
      card.classList.remove("running");
    }

    // The two independent answers, shown separately. When the contactor is
    // closed and no current follows, the motor is not turning, and that is
    // only visible if both are on screen.
    const contact = card.querySelector("[data-contact]");
    if (pump.run_contact === null) {
      contact.innerHTML = '<span class="muted">not wired</span>';
    } else if (pump.run_contact && !pump.drawing_current) {
      contact.innerHTML = '<strong class="bad">closed, no current</strong>';
    } else {
      contact.textContent = pump.run_contact ? "closed" : "open";
    }

    const overload = card.querySelector("[data-overload]");
    if (pump.overload_tripped === null) {
      overload.innerHTML = '<span class="muted">not wired</span>';
    } else if (pump.overload_tripped) {
      overload.innerHTML = '<strong class="bad">TRIPPED</strong>';
    } else {
      overload.textContent = "healthy";
    }

    card.querySelector("[data-voltage]").textContent =
      typeof pump.voltage === "number" ? pump.voltage.toFixed(1) + " V" : "--";
    card.querySelector("[data-power]").textContent =
      typeof pump.act_power === "number" ? Math.round(pump.act_power) + " W" : "--";
  }

  function renderFloats(floats) {
    document.querySelectorAll("[data-float]").forEach(function (row) {
      const key = row.getAttribute("data-float");
      const reading = (floats || {})[key];
      const pill = row.querySelector("[data-float-pill]");
      const stamp = row.querySelector("[data-float-since]");

      if (!reading || reading.state === null) {
        setPill(pill, "Not wired", "idle");
        stamp.textContent = "";
        row.classList.remove("wet");
        return;
      }
      if (reading.state) {
        setPill(pill, key === "panel_alarm" ? "ALARM" : "WET", "crit");
        row.classList.add("wet");
      } else {
        setPill(pill, key === "panel_alarm" ? "Clear" : "Dry", "ok");
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
    const high = (state.floats || {}).high_water;
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
    renderFloats(state.floats);
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
