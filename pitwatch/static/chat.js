/* The chat page: keep the newest answer in view, and say when one is coming.

   The form posts and the page reloads, which is the same shape every other
   form here uses. That costs nothing on a page you read rather than watch, and
   it means the transcript is whatever the database says rather than something
   the browser has been keeping its own copy of.

   What it does need is the two things a full page post loses: the thread
   scrolled to the bottom where the newest answer is, and some sign that a
   question which takes fifteen seconds is being worked on rather than ignored. */
(function () {
  "use strict";

  var thread = document.querySelector("[data-chat-thread]");
  if (thread) {
    /* The newest answer is at the bottom, which is where a conversation is
       read from. Jumped rather than smooth: this is the first paint after a
       reload, and animating to it looks like the page moved on its own. */
    thread.scrollTop = thread.scrollHeight;
  }

  var form = document.querySelector("[data-chat-form]");
  if (!form) {
    return;
  }

  var box = form.querySelector("textarea");
  var button = form.querySelector("button[type=submit]");

  form.addEventListener("submit", function () {
    /* Disabled after the browser has taken the value, never before: a disabled
       field is not submitted, and doing this on click would post an empty
       question. */
    if (button) {
      button.disabled = true;
      button.textContent = "Thinking...";
    }
    if (box) {
      box.readOnly = true;
    }
  });

  /* Enter sends, shift-Enter makes a new line, which is what every chat box
     does and what fingers expect. */
  if (box) {
    box.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (box.value.trim()) {
          form.requestSubmit();
        }
      }
    });
    box.focus();
  }
})();
