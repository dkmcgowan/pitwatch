// The CSRF token, for the fetch calls on this page.
//
// They post a FormData built from a real form, which the browser sends as
// multipart. The server cannot parse a multipart body without draining the
// request out from under the handler that needs it, so for those the token
// travels in a header instead. The hidden field is right there in the form
// either way.

function csrfHeader(form) {
  const field = form && form.querySelector('input[name="csrf_token"]');
  return field ? { "X-CSRF-Token": field.value } : {};
}

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
        headers: csrfHeader(form),
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

// The live input view on the panel inputs section.
//
// This is the wiring aid. Reading the labels on a panel's terminal strip is how
// a channel map ends up wrong; lifting a float and watching a row change is how
// it ends up right.
//
// It listens rather than polls, because the module speaks only when something
// changes and there is nothing to ask it. Each request waits at the broker for
// a published body and the next one starts when that one comes back, so a float
// lifted at the panel shows up here as fast as the wire carries it. A request
// that waits out its time without hearing anything is not a failure and is not
// drawn as one: it means nothing moved, and the answer is to keep listening.

(function () {
  "use strict";

  const button = document.querySelector("[data-test-inputs]");
  const output = document.querySelector("[data-inputs-result]");
  if (!button || !output) {
    return;
  }

  let listening = false;
  let inFlight = null;

  function escape(value) {
    const node = document.createElement("span");
    node.textContent = String(value === null || value === undefined ? "" : value);
    return node.innerHTML;
  }

  function stop() {
    listening = false;
    if (inFlight !== null) {
      inFlight.abort();
      inFlight = null;
    }
    button.textContent = "Listen for a change";
    output.hidden = true;
  }

  function waiting() {
    output.className = "test-result";
    output.innerHTML =
      "Listening. Lift a float or push a contactor in by hand, and the row " +
      "that changes is the input it is on.";
  }

  function render(result) {
    const rows = (result.channels || [])
      .map(function (channel) {
        // Both columns, always. The raw bit is what the wire says and the
        // state is what it means after the normally-closed setting; showing
        // only one of them is how an inverted channel goes unnoticed.
        return (
          "<tr><th scope=\"row\">DI" +
          escape(channel.channel) +
          "</th><td>" +
          (channel.label
            ? escape(channel.label)
            : "<span class=\"muted\">not wired</span>") +
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
      "<table class=\"probe\"><thead><tr><th>Input</th><th>Name</th>" +
      "<th>Contact</th><th>Reads as</th></tr></thead><tbody>" +
      rows +
      "</tbody></table>";
  }

  async function listen() {
    const form = button.closest("form");
    if (!form) {
      return;
    }

    while (listening) {
      inFlight = new AbortController();
      let text;
      let status;
      try {
        const response = await fetch("/api/test/inputs", {
          method: "POST",
          body: new FormData(form),
          headers: csrfHeader(form),
          signal: inFlight.signal
        });
        status = response.status;
        text = await response.text();
      } catch (error) {
        if (!listening) {
          return;
        }
        output.className = "test-result bad";
        output.innerHTML = "The request failed: " + escape(error.message);
        stop();
        return;
      } finally {
        inFlight = null;
      }

      if (!listening) {
        return;
      }

      let result;
      try {
        result = JSON.parse(text);
      } catch (error) {
        output.className = "test-result bad";
        output.innerHTML =
          status === 401
            ? "Sign in first."
            : "The server returned something unexpected (" + status + ").";
        stop();
        return;
      }

      if (result.ok) {
        render(result);
      } else if (result.waiting) {
        // Nothing moved. The connection is fine, so go round again.
        if (!output.innerHTML || output.className !== "test-result good") {
          waiting();
        }
      } else {
        output.className = "test-result bad";
        output.innerHTML = escape(result.error || "Could not reach the broker");
        stop();
        return;
      }
    }
  }

  button.addEventListener("click", function () {
    if (listening) {
      stop();
      return;
    }
    listening = true;
    output.hidden = false;
    waiting();
    button.textContent = "Stop listening";
    listen();
  });

  // Leaving the page with a request open would hold a broker connection for
  // nothing, and on a phone that is somebody's battery.
  window.addEventListener("pagehide", stop);
})();

// Keeping the two clamp selects opposite each other.
//
// Both are real fields and both are submitted and stored. There are only two
// clamps, so moving one almost always means moving the other, and doing it here
// saves a second click. It is a convenience only: what gets saved is whatever
// the form sends, and the server refuses a form that puts both pumps on one
// clamp. With JavaScript off both still submit correctly; they just do not move
// together.

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

  second.addEventListener("change", function () {
    first.value = opposite(second.value);
  });
})();

// The two notification test buttons, and showing only the fields that apply.
//
// Both post the form as it currently stands rather than what is saved, because
// the question a test button answers is "does what I just typed work", and
// making you save first means saving something broken to find out.
//
// These send real messages. There is no dry run, on the grounds that a test
// which does not actually deliver tests nothing worth knowing.

(function () {
  "use strict";

  function escape(value) {
    const node = document.createElement("span");
    node.textContent = String(value === null || value === undefined ? "" : value);
    return node.innerHTML;
  }

  function wire(buttonSelector, resultSelector, endpoint, sendingText) {
    const button = document.querySelector(buttonSelector);
    const output = document.querySelector(resultSelector);
    if (!button || !output) {
      return;
    }

    function show(text, kind) {
      output.hidden = false;
      output.className = "test-result " + kind;
      output.innerHTML = escape(text);
    }

    button.addEventListener("click", async function () {
      const form = button.closest("form");
      if (!form) {
        return;
      }
      button.disabled = true;
      show(sendingText, "");

      try {
        const response = await fetch(endpoint, {
          method: "POST",
          body: new FormData(form),
          headers: csrfHeader(form),
        });
        const body = await response.text();
        let result;
        try {
          result = JSON.parse(body);
        } catch (error) {
          show(
            response.status === 401
              ? "Sign in first."
              : "The server returned something unexpected (" + response.status + ").",
            "bad"
          );
          return;
        }
        if (result.ok) {
          // The note carries the difference between accepted and delivered,
          // which is the whole of what a test button can honestly promise.
          show(result.detail || "Sent.", "good");
          if (result.note) {
            const aside = document.createElement("p");
            aside.className = "muted";
            aside.textContent = result.note;
            output.appendChild(aside);
          }
        } else {
          show(result.error || "It did not send.", "bad");
        }
      } catch (error) {
        show("The request failed: " + error.message, "bad");
      } finally {
        button.disabled = false;
      }
    });
  }

  wire("[data-test-email]", "[data-email-result]", "/api/test/email", "Sending...");
  wire("[data-test-sms]", "[data-sms-result]", "/api/test/sms", "Sending...");

  // Show only the provider that is selected. Both blocks stay in the form and
  // both still submit, so switching provider and back does not lose what you
  // typed; they are only hidden.
  const provider = document.querySelector("#sms_provider");
  const blocks = Array.prototype.slice.call(document.querySelectorAll("[data-sms-provider]"));
  if (provider && blocks.length) {
    const refresh = function () {
      blocks.forEach(function (block) {
        block.hidden = block.getAttribute("data-sms-provider") !== provider.value;
      });
    };
    provider.addEventListener("change", refresh);
    refresh();
  }
})();


// A notification box is unavailable until there is somewhere to send it.
//
// An account with the email box ticked and no address is a setting that reads
// as configured and delivers nothing, which is the worst state for anything on
// an alerting page to be in. The field says which box depends on it and the box
// follows what is typed, live, so it is never on without an address behind it.
//
// The server checks this again. This makes the mistake hard to make; it is not
// what makes it impossible.

(function () {
  "use strict";

  const fields = Array.prototype.slice.call(document.querySelectorAll("[data-requires]"));
  if (!fields.length) {
    return;
  }

  fields.forEach(function (field) {
    const box = document.getElementById(field.getAttribute("data-requires"));
    if (!box) {
      return;
    }
    const label = box.closest("label");

    function refresh() {
      const ready = field.value.trim() !== "";
      box.disabled = !ready;
      if (!ready) {
        // Cleared as well as disabled. A disabled checkbox posts nothing, so
        // leaving it ticked would show a state the next save would drop.
        box.checked = false;
      }
      if (label) {
        label.classList.toggle("unavailable", !ready);
        label.title = ready ? "" : "Add an address or number first";
      }
    }

    field.addEventListener("input", refresh);
    refresh();
  });
})();


// Asking before something cannot be undone.
//
// A real dialog rather than the browser's confirm box, which is styled by the
// browser, cannot say which account it means in the page's own voice, and
// blocks the whole tab while it is up. This one names the account and offers
// two buttons that say what they do rather than OK and Cancel.
//
// Without a dialog element the form keeps its normal behavior and submits, so
// the action still works; it just does not ask first.

(function () {
  "use strict";

  const dialog = document.getElementById("confirm");
  const forms = Array.prototype.slice.call(document.querySelectorAll("[data-confirm]"));
  if (!dialog || !forms.length || typeof dialog.showModal !== "function") {
    return;
  }

  const body = dialog.querySelector("[data-confirm-body]");
  const yes = dialog.querySelector("[data-confirm-yes]");
  const no = dialog.querySelector("[data-confirm-no]");
  let asking = null;

  forms.forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      asking = form;
      if (body) {
        body.textContent = form.getAttribute("data-confirm");
      }
      dialog.showModal();
    });
  });

  function dismiss() {
    asking = null;
    dialog.close();
  }

  if (yes) {
    yes.addEventListener("click", function () {
      const form = asking;
      dismiss();
      if (form) {
        // submit() does not raise the submit event, so this does not come back
        // round to the handler above and ask again.
        form.submit();
      }
    });
  }
  if (no) {
    no.addEventListener("click", dismiss);
  }

  // The backdrop, and Escape, both mean no. Clicking inside does not, because
  // there are two buttons in there and one of them deletes something.
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) {
      dismiss();
    }
  });
  dialog.addEventListener("close", function () {
    asking = null;
  });
})();


// A checkbox in a table that saves itself.
//
// Without this the box posts nothing until something submits the form, so the
// markup carries a real button and works with scripting off. With scripting
// on the button is redundant and in the way of a tidy column, so it goes and
// the box carries the change instead.

(function () {
  "use strict";

  const boxes = Array.prototype.slice.call(document.querySelectorAll("[data-autosubmit]"));
  if (!boxes.length) {
    return;
  }

  boxes.forEach(function (box) {
    const form = box.closest("form");
    if (!form) {
      return;
    }
    const button = form.querySelector("[data-autosubmit-go]");
    if (button) {
      button.hidden = true;
    }
    box.addEventListener("change", function () {
      // Disabled straight away, so a second click while the page is still
      // reloading cannot send a second flip and undo the first.
      box.disabled = true;
      form.submit();
    });
  });
})();
