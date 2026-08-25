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

  function render(result) {
    if (!result.ok) {
      show(escape(result.error || "Could not reach the device"), "bad");
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
          " (em1:" +
          channel +
          ")</th><td class=\"mono\">" +
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

    show(
      "<p>Connected to <strong>" +
        escape(result.model || result.id || "the device") +
        "</strong>, firmware " +
        escape(result.firmware || "unknown") +
        ".</p><table class=\"probe\"><tbody>" +
        rows +
        "</tbody></table><p class=\"hint\">Start a pump by hand and watch which " +
        "reading moves. That clamp is the one on that pump.</p>",
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
