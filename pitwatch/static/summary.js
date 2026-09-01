// The summary page.
//
// One job: say something is happening while the call is out. Writing a summary
// is a round trip to somebody else's model and can take the better part of a
// minute, and a button that looks exactly the same after it is pressed is a
// button that gets pressed again.
//
// The form still posts normally. This changes nothing about how it works, only
// what it looks like while it is working, so a browser with the script blocked
// still gets a summary.

(function () {
  "use strict";

  const form = document.querySelector("[data-summary-form]");
  const button = document.querySelector("[data-summary-button]");
  if (!form || !button) {
    return;
  }

  form.addEventListener("submit", function () {
    button.textContent = "Reading the week...";
    // Disabled after the submit is under way rather than before it: a disabled
    // button on a form that has not posted yet is a form that never posts.
    window.setTimeout(function () {
      button.disabled = true;
    }, 0);
  });
})();
