// Tiny progressive-enhancement script for the Lecture Recorder UI.
(function () {
  "use strict";

  function getCookie(name) {
    var match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
    return match ? decodeURIComponent(match[1]) : "";
  }

  window.LR = window.LR || {
    config: { csrfToken: getCookie("lr_csrf") || "" },
  };

  // --- Clickable transcripts ------------------------------------------
  // [12:34] buttons (templates/_macros.html transcript_view) play the
  // recording from there; the line being played is highlighted. The
  // handlers are delegated, so panes loaded by soft navigation work too.

  function audioFor(el) {
    var scope = (el && el.closest(".lr-detail")) || document;
    return scope.querySelector("audio") || document.querySelector("audio");
  }

  function playFrom(audio, seconds) {
    function go() {
      try { audio.currentTime = seconds; } catch (e) { return; }
      var p = audio.play();
      if (p && p.catch) p.catch(function () { /* autoplay blocked */ });
    }
    // Seeking needs the metadata on some browsers.
    if (audio.readyState >= 1) go();
    else audio.addEventListener("loadedmetadata", go, { once: true });
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".lr-ts");
    if (!btn) return;
    var audio = audioFor(btn);
    if (audio) playFrom(audio, parseFloat(btn.getAttribute("data-t")) || 0);
  });

  // Media events do not bubble, but they can be caught on the way down.
  document.addEventListener("timeupdate", function (e) {
    var audio = e.target;
    var scope = audio.closest(".lr-detail") || document;
    var lines = scope.querySelectorAll(".lr-tline[data-t]");
    var current = null;
    for (var i = 0; i < lines.length; i++) {
      if (parseFloat(lines[i].getAttribute("data-t")) <= audio.currentTime) {
        current = lines[i];
      } else {
        break;
      }
    }
    for (var j = 0; j < lines.length; j++) {
      lines[j].classList.toggle("is-playing", lines[j] === current);
    }
  }, true);

  // --- Delete a recording ----------------------------------------------
  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".lr-delete-rec");
    if (!btn) return;
    var ok = window.confirm(
      "Delete this recording? Its audio, transcript and summary are " +
      "removed for good. To-dos, deadlines and calendar entries filed " +
      "from it stay."
    );
    if (!ok) return;
    btn.disabled = true;
    fetch("/api/v1/sessions/" + encodeURIComponent(btn.getAttribute("data-session-id")),
          { method: "DELETE", credentials: "same-origin" })
      .then(function (r) {
        if (r.ok) {
          // Back to the list this recording was opened from.
          var path = window.location.pathname;
          window.location.href = path.indexOf("/sessions/") === 0 ? "/recordings" : path;
          return;
        }
        btn.disabled = false;
        window.alert(r.status === 409
          ? "It is being processed right now; try again in a minute."
          : "Delete failed (" + r.status + ").");
      })
      .catch(function () {
        btn.disabled = false;
        window.alert("Network error; nothing was deleted.");
      });
  });

  // ?t=<ms> (from a to-do, deadline or search result): open the
  // transcript at that line and cue the audio there (not autoplay:
  // browsers block it on load). From a search without a moment (?hl=),
  // show the first marked word instead.
  document.addEventListener("DOMContentLoaded", function () {
    var raw = new URLSearchParams(window.location.search).get("t");
    var ms = raw === null ? NaN : parseInt(raw, 10);
    if (!isFinite(ms) || ms < 0) {
      var mark = document.querySelector(".lr-detail mark");
      if (mark) {
        var closed = mark.closest("details");
        if (closed) closed.open = true;
        mark.scrollIntoView({ block: "center" });
      }
      return;
    }
    var seconds = ms / 1000;
    var audio = audioFor(document.querySelector(".lr-detail"));
    if (!audio) return;
    function cue() {
      try { audio.currentTime = seconds; } catch (e) { /* ignore */ }
    }
    if (audio.readyState >= 1) cue();
    else audio.addEventListener("loadedmetadata", cue, { once: true });
    var line = null;
    document.querySelectorAll(".lr-tline[data-t]").forEach(function (l) {
      if (parseFloat(l.getAttribute("data-t")) <= seconds) line = l;
    });
    if (line) {
      var details = line.closest("details");
      if (details) details.open = true;
      line.classList.add("is-playing");
      line.scrollIntoView({ block: "center" });
    }
  });
})();
