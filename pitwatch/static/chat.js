/* The chat page: watch for the answer, and get out of the way.

   The question is posted and the page comes straight back, because the model
   takes tens of seconds and a request held open that long is a 524 from
   Cloudflare rather than an answer. So the answer arrives here instead: the
   reserved row is drawn as a thinking mark, this asks the server for the thread
   every couple of seconds, and swaps the mark for the text when it lands.

   Nothing is kept in the browser. The thread is whatever the database says,
   which is why a reload mid-thought shows the thinking rather than an empty
   page, and why two tabs agree. */
(function () {
  "use strict";

  var page = document.querySelector("[data-chat]");
  if (!page) {
    return;
  }

  var thread = page.querySelector("[data-chat-thread]");
  var form = page.querySelector("[data-chat-form]");
  var box = form && form.querySelector("textarea");

  /* How often to ask, and how long before giving up on asking.
     Two seconds is often enough to feel immediate without being a request per
     second per open tab. The ceiling is above the server's own timeout, so the
     failure it writes is seen rather than waited past. */
  var EVERY_MS = 2000;
  var GIVE_UP_MS = 150000;

  function atBottom() {
    return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
  }

  function toBottom() {
    thread.scrollTop = thread.scrollHeight;
  }

  /* Drawn here as well as on the server, because an answer that arrives
     without a reload has to look the same as one that was there on load. */
  function draw(said) {
    var empty = thread.querySelector(".chat-empty");
    if (empty && said.length) {
      empty.remove();
    }
    said.forEach(function (line) {
      var el = thread.querySelector('[data-line="' + line.id + '"]');
      if (!el) {
        el = document.createElement("article");
        el.setAttribute("data-line", line.id);
        el.appendChild(document.createElement("div")).className = "chat-said";
        thread.appendChild(el);
      }
      el.className =
        "chat-line chat-" + line.role + (line.failed ? " chat-failed" : "");
      var said_ = el.querySelector(".chat-said");
      if (line.pending) {
        if (!said_.querySelector(".chat-thinking")) {
          said_.innerHTML = '<span class="chat-thinking" aria-label="Thinking">' +
            "<i></i><i></i><i></i></span>";
        }
      } else if (said_.textContent !== line.content) {
        /* textContent, never innerHTML: this came from a model and a model's
           output is not markup. */
        said_.textContent = line.content;
      }
    });
  }

  var began = Date.now();
  var timer = null;

  function watch() {
    fetch("/api/chat", { headers: { Accept: "application/json" } })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (state) {
        if (!state) {
          return;
        }
        var wasDown = atBottom();
        draw(state.said);
        if (wasDown) {
          toBottom();
        }
        if (!state.waiting) {
          stop();
          if (box) {
            box.disabled = false;
            box.focus();
          }
        } else if (Date.now() - began > GIVE_UP_MS) {
          stop();
        }
      })
      .catch(function () {
        /* A dropped poll is not worth saying anything about. The next one is
           two seconds away, and the answer is in the database either way. */
      });
  }

  function stop() {
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  if (thread) {
    toBottom();
    if (thread.querySelector(".chat-thinking")) {
      /* Something was already being written when this page loaded, whether
         this tab asked for it or another one did. */
      if (box) {
        box.disabled = true;
      }
      timer = window.setInterval(watch, EVERY_MS);
    }
  }

  /* The box grows with what is typed, up to a point, the way every chat box
     does. Reset first or it can only ever get taller. */
  function fit() {
    box.style.height = "auto";
    box.style.height = Math.min(box.scrollHeight, 200) + "px";
  }

  if (box) {
    box.addEventListener("input", fit);
    box.focus();

    /* Enter sends, shift-Enter makes a new line. */
    box.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        if (box.value.trim()) {
          form.requestSubmit();
        }
      }
    });
  }

  page.querySelectorAll("[data-starter]").forEach(function (button) {
    button.addEventListener("click", function () {
      box.value = button.textContent.trim();
      fit();
      form.requestSubmit();
    });
  });
})();
