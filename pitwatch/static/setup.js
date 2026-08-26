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
        headers: csrfHeader(form),
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

// Editing the list of signals the panel brings out.
//
// The eight this ships with are what a duplex ejector panel usually has, not a
// limit. Panels differ, so the list is a setting like any other and this keeps
// the channel dropdowns below in step with it as it is edited, rather than
// making somebody save and reload to find out what they can now pick.
//
// The key is what the database holds and the label is what people read, which
// is why a row that is already saved posts its key back untouched however it is
// renamed. Only a row added here gets its key from what is typed, and it is
// built the same way the server would build it, so the two agree without having
// to be told about each other.
//
// With JavaScript off, all of this still works: the rows are ordinary inputs,
// emptying a name removes that signal, and the server does the rest.

(function () {
  "use strict";

  const rows = document.querySelector("[data-signal-rows]");
  const add = document.querySelector("[data-add-signal]");
  if (!rows || !add) {
    return;
  }

  const UNUSED = "unused";

  function selects() {
    return Array.prototype.slice.call(
      document.querySelectorAll('select[name^="channel_"][name$="_signal"]')
    );
  }

  function announce() {
    document.dispatchEvent(new CustomEvent("pitwatch:signals-changed"));
  }

  // The same rule as signal_key() in schemas.py. If these two ever disagree, a
  // signal added and wired to an input in one visit is refused on save.
  function slug(label) {
    let cleaned = label
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "");
    if (cleaned && !/^[a-z]/.test(cleaned)) {
      cleaned = "s_" + cleaned;
    }
    return cleaned.slice(0, 40).replace(/_+$/, "") || "signal";
  }

  function keysInUse(except) {
    const used = [];
    Array.prototype.forEach.call(
      rows.querySelectorAll('input[name="signal_key"]'),
      function (input) {
        if (input !== except && input.value) {
          used.push(input.value);
        }
      }
    );
    return used;
  }

  function uniqueKey(base, except) {
    const used = keysInUse(except);
    if (base !== UNUSED && used.indexOf(base) === -1) {
      return base;
    }
    const stem = base === UNUSED ? "signal" : base;
    let n = 2;
    while (used.indexOf(stem + "_" + n) !== -1) {
      n += 1;
    }
    return stem + "_" + n;
  }

  function optionFor(select, key) {
    return Array.prototype.filter.call(select.options, function (option) {
      return option.value === key;
    })[0];
  }

  function putOption(key, label) {
    selects().forEach(function (select) {
      let option = optionFor(select, key);
      if (!option) {
        option = document.createElement("option");
        option.value = key;
        select.appendChild(option);
      }
      option.textContent = label;
    });
  }

  function dropOption(key) {
    selects().forEach(function (select) {
      const option = optionFor(select, key);
      if (!option) {
        return;
      }
      // Freeing the input first. Removing the option a select is sitting on
      // leaves it showing a blank and posting whatever the browser decides.
      if (select.value === key) {
        select.value = UNUSED;
      }
      option.remove();
    });
  }

  function wire(row) {
    const keyInput = row.querySelector('input[name="signal_key"]');
    const labelInput = row.querySelector('input[name="signal_label"]');
    const remove = row.querySelector("[data-remove-signal]");
    const fresh = !keyInput.value;

    labelInput.addEventListener("input", function () {
      const label = labelInput.value.trim();
      if (fresh) {
        const previous = keyInput.value;
        const next = label ? uniqueKey(slug(label), keyInput) : "";
        if (previous && previous !== next) {
          dropOption(previous);
        }
        keyInput.value = next;
      }
      if (keyInput.value) {
        putOption(keyInput.value, label || keyInput.value);
      }
      announce();
    });

    remove.addEventListener("click", function () {
      if (keyInput.value) {
        dropOption(keyInput.value);
      }
      row.remove();
      announce();
    });
  }

  Array.prototype.forEach.call(rows.querySelectorAll("tr"), wire);

  add.addEventListener("click", function () {
    const row = document.createElement("tr");
    row.innerHTML =
      '<td><input type="hidden" name="signal_key" value="">' +
      '<input type="text" name="signal_label" value="" maxlength="60"' +
      ' aria-label="Name of the new signal"></td>' +
      '<td class="muted">not wired</td>' +
      '<td class="center"><button type="button" class="button secondary small"' +
      " data-remove-signal>Remove</button></td>";
    rows.appendChild(row);
    wire(row);
    row.querySelector('input[name="signal_label"]').focus();
  });
})();

// Taking used signals out of the other channels' dropdowns.
//
// A signal belongs to exactly one input. Offering "Lead float" on all eight
// channels when it is already on DI1 invites the duplicate that the server then
// refuses, which is a worse way to find out than simply not being able to pick
// it. So each select hides the signals that are spoken for elsewhere.
//
// Two things stay pickable everywhere: "Not connected", because any number of
// inputs can be unused, and whatever this select is currently set to, because
// hiding a select's own value is how a dropdown ends up showing a blank.
//
// The server still checks for duplicates. This makes the mistake hard to make;
// it is not what makes it impossible, and with JavaScript off the check is
// still the thing that catches it.

(function () {
  "use strict";

  const UNUSED = "unused";
  const selects = Array.prototype.slice.call(
    document.querySelectorAll('select[name^="channel_"][name$="_signal"]')
  );
  if (selects.length < 2) {
    return;
  }

  function refresh() {
    const taken = new Set(
      selects.map((select) => select.value).filter((value) => value !== UNUSED)
    );

    selects.forEach(function (select) {
      Array.prototype.forEach.call(select.options, function (option) {
        const spokenFor =
          option.value !== UNUSED &&
          option.value !== select.value &&
          taken.has(option.value);
        option.hidden = spokenFor;
        // Hidden alone is not enough: a keyboard user can still reach a hidden
        // option in some browsers, and it would submit.
        option.disabled = spokenFor;
      });
    });
  }

  selects.forEach(function (select) {
    select.addEventListener("change", refresh);
  });

  // The list of signals is editable above, so the options can change under
  // this without any select being touched.
  document.addEventListener("pitwatch:signals-changed", refresh);

  refresh();
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
          show(result.detail || "Sent.", "good");
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
