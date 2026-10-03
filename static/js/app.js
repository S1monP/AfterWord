// Tiny progressive-enhancement script for the Lecture Recorder UI.
// Phase A: refresh the page when the status changes for an in-progress
// session so the UI moves from "receiving" to "ready" without a manual
// reload. Later phases can extend this to live updates for transcripts.
(function () {
  "use strict";

  function getCookie(name) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : "";
  }

  // Placeholder for future use (e.g. session-detail polling).
  window.LR = window.LR || {
    config: { csrfToken: getCookie("lr_csrf") || "" },
  };
})();
