/* Upload: pick or drop one audio file, send it with a progress bar, then
   open the new recording. The server converts it (ffmpeg) and starts
   processing; the browser only sends the file as it is. */
(function () {
  "use strict";
  var form = document.querySelector("[data-upload]");
  if (!form) return;
  var drop = form.querySelector("[data-drop]"), input = form.querySelector("[data-file]");
  var nameEl = form.querySelector("[data-file-name]"), sizeEl = form.querySelector("[data-file-size]");
  var submit = form.querySelector("[data-submit]"), progress = form.querySelector("[data-progress]");
  var bar = form.querySelector("[data-bar]"), progressText = form.querySelector("[data-progress-text]");
  var errorBox = form.querySelector("[data-error]"), errorText = form.querySelector("[data-error-text]");
  var max = +form.dataset.max, chosen = null, busy = false;
  var REASONS = {
    file_too_large: "The file is larger than the server accepts.",
    empty_file: "The file is empty.",
    transcode_failed: "The server couldn't read this file. Is it audio?",
    ffmpeg_missing: "This file needs ffmpeg on the server.",
    missing_file: "No file arrived. Try again.",
    invalid_title: "That name can't be used. Try a shorter one.",
  };

  function mb(bytes) { return (bytes / 1024 / 1024).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0) + " MB"; }
  function showError(text) { errorText.textContent = text; errorBox.hidden = !text; }

  function choose(file) {
    showError("");
    chosen = file || null;
    drop.classList.toggle("has-file", !!chosen);
    if (!chosen) { submit.disabled = true; return; }
    nameEl.textContent = chosen.name;
    sizeEl.textContent = mb(chosen.size) + " · choose another";
    if (chosen.size > max) { showError("This file is " + mb(chosen.size) + "; the limit is " + mb(max) + "."); submit.disabled = true; return; }
    submit.disabled = false;
  }
  input.addEventListener("change", function () { choose(input.files[0]); });
  ["dragenter", "dragover"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("is-over"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("is-over"); });
  });
  drop.addEventListener("drop", function (e) {
    var files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    if (files.length > 1) { showError("One file at a time, please."); return; }
    choose(files[0]);
  });
  // Leaving mid-upload loses it.
  window.addEventListener("beforeunload", function (e) { if (busy) { e.preventDefault(); e.returnValue = ""; } });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!chosen || busy) return;
    var data = new FormData();
    data.append("file", chosen);
    var title = form.elements["title"].value.trim();
    if (title) data.append("title", title);
    busy = true; submit.disabled = true; showError("");
    progress.hidden = false; bar.style.width = "0%"; progressText.textContent = "Uploading…";
    var xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.upload.onprogress = function (ev) {
      if (!ev.lengthComputable) return;
      var pct = Math.round(ev.loaded / ev.total * 100);
      bar.style.width = pct + "%";
      progressText.textContent = pct < 100 ? "Uploading… " + pct + "%" : "Converting and starting the transcription…";
    };
    xhr.onload = function () {
      var body = {};
      try { body = JSON.parse(xhr.responseText || "{}"); } catch (err) {}
      busy = false;
      if (xhr.status >= 200 && xhr.status < 300 && body.redirect_url) {
        window.AW.toastNext("Uploaded. Transcribing now.");
        location.href = body.redirect_url;
        return;
      }
      progress.hidden = true; submit.disabled = false;
      showError(REASONS[body.error] || body.detail || ("The server said " + xhr.status + "."));
    };
    xhr.onerror = function () {
      busy = false; progress.hidden = true; submit.disabled = false;
      showError("The connection dropped. Check the network and try again.");
    };
    xhr.send(data);
  });
})();
