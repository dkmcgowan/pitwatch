// The information notes, on every page that has an i beside a heading.
//
// A native dialog, opened as a modal. It renders in the top layer, so it
// cannot push a card around or end up behind one whatever the stacking looks
// like, and Escape closes it without being told to. Clicking anywhere closes
// it, including inside: there is nothing in there to interact with, and a note
// that needs a target found before it will go away is a note that gets left
// open.
//
// Only ever one at a time, because that is what showModal means.
//
// Loaded by every page rather than by the dashboard alone. It was the
// dashboard's, and the history page needed the same four lines the day it was
// written, which is one copy too many.

(function () {
  "use strict";

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
})();
