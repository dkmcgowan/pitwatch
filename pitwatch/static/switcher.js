/* The building picker in the header.

   In a file rather than an `onchange` attribute because the Content Security
   Policy is `script-src 'self'`, which blocks inline handlers. One written
   inline renders perfectly, looks right in a screenshot, and silently does
   nothing when somebody picks a building: the browser refuses it and the only
   sign is a line in a console nobody has open. That is exactly how this was
   found, and it is why the noscript button below it is not decoration.

   Only ever present on an installation with more than one building. */
(function () {
  "use strict";

  var select = document.getElementById("site-switch");
  if (!select || !select.form) {
    return;
  }

  select.addEventListener("change", function () {
    select.form.submit();
  });

  /* The fallback button is for somebody with no JavaScript at all. This file
     running is the proof that they have some, so it goes. */
  var fallback = select.form.querySelector("button[type=submit]");
  if (fallback) {
    fallback.hidden = true;
  }
})();
