// The test connection button on the Shelly section.
//
// It posts the form as it currently stands rather than what is saved, because
// the moment it is most useful is before anything has been saved. The reply
// carries the live current on both clamps, so someone standing at the panel can
// start a pump by hand and see which of the two numbers moves. That is the
// answer to the one question the hardware cannot answer for itself.

(function () {
  "use strict";

  const button = document.querySelector("[data-test-shelly]");
  const output = document.querySelector("[data-shelly-result]");
  if (!button || !output) {
    return;
  }

  const amps = (value) =>
    typeof value === "number" ? value.toFixed(2) + " A" : "no reading";

  function show(html, kind) {
    output.hidden = false;
    output.className = "test-result " + kind;
    output.innerHTML = html;
  }

  function escape(value) {
    const node = document.createElement("span");
    node.textContent = String(value === null || value === undefined ? "" : value);
    return node.innerHTML;
  }

  function renderSteps(steps) {
    if (!steps || !steps.length) {
      return "";
    }
    // Every rung that was tried, in order, with the one that stopped it marked.
    // Where it stops is the diagnosis, so all of them are shown rather than
    // only the failure.
    return (
      '<table class="probe steps"><tbody>' +
      steps
        .map(function (step) {
          return (
            "<tr><td>" +
            (step.ok ? '<span class="on">OK</span>' : '<span class="bad">FAILED</span>') +
            "</td><th scope=\"row\">" +
            escape(step.step) +
            "</th><td class=\"muted\">" +
            escape(step.detail) +
            "</td></tr>"
          );
        })
        .join("") +
      "</tbody></table>"
    );
  }

  function showClampReadings(channels) {
    [1, 2].forEach(function (pump) {
      const select = document.querySelector('[data-clamp="' + pump + '"]');
      const cell = document.querySelector('[data-clamp-reading="' + pump + '"]');
      if (!select || !cell) {
        return;
      }
      const reading = channels[select.value] || {};
      cell.textContent =
        typeof reading.current === "number" ? "now " + amps(reading.current) : "";
    });
  }

  function render(result) {
    if (!result.ok) {
      show(
        renderSteps(result.steps) +
          "<p>" +
          escape(result.error || "Could not reach the device") +
          "</p>",
        "bad"
      );
      return;
    }
    const rows = [0, 1]
      .map((channel) => {
        const reading = (result.channels || {})[channel] || {};
        const errors = (reading.errors || []).length
          ? ' <span class="bad">' + escape(reading.errors.join(", ")) + "</span>"
          : "";
        return (
          "<tr><th scope=\"row\">Clamp " +
          (channel + 1) +
          "</th><td class=\"mono\">" +
          escape(amps(reading.current)) +
          "</td><td class=\"mono\">" +
          escape(
            typeof reading.voltage === "number"
              ? reading.voltage.toFixed(1) + " V"
              : "no reading"
          ) +
          "</td><td>" +
          errors +
          "</td></tr>"
        );
      })
      .join("");

    // Put each clamp's live current next to the pump it is currently assigned
    // to. Reading two numbers in a table and mapping them back onto two
    // dropdowns is exactly the kind of small translation that gets done wrong
    // at the end of a long day in a boiler room.
    showClampReadings(result.channels || {});

    show(
      renderSteps(result.steps) +
        "<p>Connected to <strong>" +
        escape(result.model || result.id || "the device") +
        "</strong>, firmware " +
        escape(result.firmware || "unknown") +
        ".</p><table class=\"probe\"><tbody>" +
        rows +
        "</tbody></table><p class=\"hint\">Start a pump by hand and watch which " +
        "reading moves. That clamp is the one on that pump; set it above.</p>",
      "good"
    );
  }

  button.addEventListener("click", async function () {
    const form = button.closest("form");
    if (!form) {
      return;
    }

    button.disabled = true;
    show("Connecting...", "");

    try {
      const response = await fetch("/api/test/shelly", {
        method: "POST",
        body: new FormData(form),
      });
      // A 401 arrives as HTML, not JSON, so read defensively.
      const text = await response.text();
      let result;
      try {
        result = JSON.parse(text);
      } catch (error) {
        show(
          response.status === 401
            ? "Sign in first."
            : "The server returned something unexpected (" + response.status + ").",
          "bad"
        );
        return;
      }
      render(result);
    } catch (error) {
      show("The request failed: " + escape(error.message), "bad");
    } finally {
      button.disabled = false;
    }
  });
})();

// The live input view on the Waveshare section.
//
// This is the wiring aid. Reading the labels on a panel's terminal strip is how
// a channel map ends up wrong; lifting a float and watching a row change is how
// it ends up right. It polls while it is open and stops when it is closed,
// because there is no reason to keep asking a Modbus device for eight bits
// after somebody has walked away from the page.

(function () {
  "use strict";

  const button = document.querySelector("[data-test-waveshare]");
  const output = document.querySelector("[data-waveshare-result]");
  if (!button || !output) {
    return;
  }

  const POLL_MS = 500;
  let timer = null;

  function escape(value) {
    const node = document.createElement("span");
    node.textContent = String(value === null || value === undefined ? "" : value);
    return node.innerHTML;
  }

  function stop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
    button.textContent = "Watch the inputs live";
    output.hidden = true;
  }

  function render(result) {
    if (!result.ok) {
      output.className = "test-result bad";
      output.innerHTML = escape(result.error || "Could not read the module");
      return;
    }
    const rows = (result.channels || [])
      .map(function (channel) {
        // Both columns, always. The raw bit is what the wire says and the
        // state is what it means after the normally-closed setting; showing
        // only one of them is how an inverted channel goes unnoticed.
        return (
          "<tr><th scope=\"row\">DI" +
          escape(channel.channel) +
          "</th><td>" +
          escape(channel.label) +
          "</td><td class=\"mono\">" +
          (channel.raw ? "closed" : "open") +
          "</td><td>" +
          (channel.state
            ? "<strong class=\"on\">ON</strong>"
            : "<span class=\"muted\">off</span>") +
          "</td></tr>"
        );
      })
      .join("");

    output.className = "test-result good";
    output.innerHTML =
      "<table class=\"probe\"><thead><tr><th>Input</th><th>Mapped to</th>" +
      "<th>Contact</th><th>Reads as</th></tr></thead><tbody>" +
      rows +
      "</tbody></table>";
  }

  async function poll() {
    const form = button.closest("form");
    if (!form) {
      return;
    }
    try {
      const response = await fetch("/api/test/waveshare", {
        method: "POST",
        body: new FormData(form),
      });
      const text = await response.text();
      try {
        render(JSON.parse(text));
      } catch (error) {
        output.className = "test-result bad";
        output.innerHTML =
          response.status === 401
            ? "Sign in first."
            : "The server returned something unexpected (" + response.status + ").";
        stop();
      }
    } catch (error) {
      output.className = "test-result bad";
      output.innerHTML = "The request failed: " + escape(error.message);
    }
  }

  button.addEventListener("click", function () {
    if (timer !== null) {
      stop();
      return;
    }
    output.hidden = false;
    output.className = "test-result";
    output.innerHTML = "Reading...";
    button.textContent = "Stop watching";
    poll();
    timer = setInterval(poll, POLL_MS);
  });

  // Leaving the page with a timer running would keep polling a device for
  // nothing, and on a phone that is somebody's battery.
  window.addEventListener("pagehide", stop);
})();

// Keeping the two clamp selects opposite each other.
//
// The form only ever submits pump 1, and the server derives pump 2 from it, so
// there is no state here that can be wrong. This exists purely so the page
// shows both pumps and lets you change either one, rather than telling you
// about one and leaving the other to be inferred, which reads like a trick.
// With JavaScript off, pump 1 still submits correctly and pump 2 still displays
// the right thing; it just will not move on its own.

(function () {
  "use strict";

  const first = document.querySelector('[data-clamp="1"]');
  const second = document.querySelector('[data-clamp="2"]');
  if (!first || !second) {
    return;
  }

  function opposite(value) {
    return value === "0" ? "1" : "0";
  }

  first.addEventListener("change", function () {
    second.value = opposite(first.value);
  });

  // Changing pump 2 is really changing pump 1, since only one of them is a
  // stored setting. Doing it this way round means the control that looks
  // editable is editable.
  second.addEventListener("change", function () {
    first.value = opposite(second.value);
  });
})();
