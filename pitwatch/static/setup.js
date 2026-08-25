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
