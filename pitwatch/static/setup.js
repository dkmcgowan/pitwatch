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

// The two notification test buttons.
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

// The address lookup on the site section.
//
// It fills the two coordinate boxes rather than saving anything, and it prints
// what the geocoder matched. That is the whole reason it does not save: a
// lookup that finds the right street in the wrong state is the failure that
// matters, and the only way to catch it is for a person to read the answer
// before committing to it.

(function () {
  "use strict";

  const button = document.querySelector("[data-geocode]");
  const output = document.querySelector("[data-geocode-result]");
  const address = document.querySelector("#site_address");
  const latitude = document.querySelector("#site_latitude");
  const longitude = document.querySelector("#site_longitude");
  const located = document.querySelector("[data-located]");
  if (!button || !output || !address || !latitude || !longitude) {
    return;
  }

  function show(text, kind) {
    output.hidden = false;
    output.className = "test-result " + kind;
    output.textContent = text;
  }

  function note(text) {
    const aside = document.createElement("p");
    aside.className = "muted";
    aside.textContent = text;
    output.appendChild(aside);
  }

  button.addEventListener("click", async function () {
    const form = button.closest("form");
    if (!form || !address.value.trim()) {
      show("Type an address first.", "bad");
      return;
    }
    button.disabled = true;
    show("Looking it up...", "");

    try {
      const body = new FormData();
      body.append("site_address", address.value);
      const response = await fetch("/api/geocode", {
        method: "POST",
        body: body,
        headers: csrfHeader(form),
      });
      let result;
      try {
        result = JSON.parse(await response.text());
      } catch (error) {
        show(
          response.status === 401
            ? "Sign in first."
            : "The server returned something unexpected (" + response.status + ").",
          "bad"
        );
        return;
      }
      if (!result.ok) {
        show(result.error || "Nothing found.", "bad");
        return;
      }
      latitude.value = result.latitude;
      longitude.value = result.longitude;
      if (located) {
        located.value = result.located || "";
      }
      // The matched place is the thing to read, so it is the headline rather
      // than the coordinates: nobody can check a pair of numbers by eye.
      show(result.located || "Found it.", "good");
      if (result.note) {
        note(result.note);
      }
      note("Not saved yet. Press Save to keep it.");
    } catch (error) {
      show("The request failed: " + error.message, "bad");
    } finally {
      button.disabled = false;
    }
  });
})();

// The nearest tide gauge, on the tide section.
//
// Same arrangement as the address lookup above and for the same reason: it
// fills the boxes and prints the name, and a person reads it before saving. The
// nearest gauge by distance is not always the same water as the pit, so the
// name is the thing to check, not the number.

(function () {
  "use strict";

  const button = document.querySelector("[data-tide-find]");
  const note = document.querySelector("[data-tide-note]");
  const station = document.querySelector("#tide_station");
  const name = document.querySelector("#tide_station_name");
  if (!button || !station || !name) {
    return;
  }

  function say(text) {
    if (note) {
      note.textContent = text;
    }
  }

  button.addEventListener("click", async function () {
    const form = button.closest("form");
    if (!form) {
      return;
    }
    button.disabled = true;
    say("Looking...");

    try {
      const response = await fetch("/api/tide/nearest", {
        method: "POST",
        body: new FormData(),
        headers: csrfHeader(form),
      });
      let result;
      try {
        result = JSON.parse(await response.text());
      } catch (error) {
        say(
          response.status === 401
            ? "Sign in first."
            : "The server returned something unexpected (" + response.status + ")."
        );
        return;
      }
      if (!result.ok) {
        say(result.error || "Nothing found.");
        return;
      }
      station.value = result.station || "";
      name.value = result.name || "";
      say((result.note || "Found it.") + " Not saved yet, press Save to keep it.");
    } catch (error) {
      say("The request failed: " + error.message);
    } finally {
      button.disabled = false;
    }
  });
})();
