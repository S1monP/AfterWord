// Upload page logic.
// Phase A: no client-side transcoding. We just send the file to the server
// as-is; the server uses ffmpeg (if available) to turn it into the
// canonical 22050 Hz mono 32-bit WAV. Keeping the browser path minimal
// means we don't have to chase cross-browser Web Audio quirks for
// iPhone M4A files.
(function () {
  "use strict";

  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("file-input");
  var fileName = document.getElementById("file-name");
  var titleInput = document.getElementById("title-input");
  var submitBtn = document.getElementById("submit-btn");
  var status = document.getElementById("upload-status");
  var form = document.getElementById("upload-form");

  if (!dropzone || !fileInput) return;

  var chosen = null;

  function pad(n) { return n < 10 ? "0" + n : "" + n; }

  function defaultTitle() {
    var d = new Date();
    var date = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
    var time = pad(d.getHours()) + pad(d.getMinutes());
    return "rec_" + date + "T" + time;
  }

  function setFile(file) {
    chosen = file;
    if (!file) {
      fileName.textContent = "No file selected";
      submitBtn.disabled = true;
      return;
    }
    fileName.textContent = file.name + " (" + Math.round(file.size / 1024) + " KB)";
    submitBtn.disabled = false;
    if (!titleInput.value) {
      titleInput.value = defaultTitle();
    }
    dropzone.classList.remove("lr-dropzone--error");
  }

  function setStatus(msg, kind) {
    status.textContent = msg || "";
    status.style.color = kind === "error"
      ? "var(--lr-danger)"
      : (kind === "ok" ? "var(--lr-success)" : "var(--lr-text-muted)");
  }

  // --- Drop zone wiring ---
  dropzone.addEventListener("click", function () { fileInput.click(); });
  dropzone.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });

  ["dragenter", "dragover"].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.add("lr-dropzone--active");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dropzone.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove("lr-dropzone--active");
    });
  });
  dropzone.addEventListener("drop", function (e) {
    var dt = e.dataTransfer;
    if (!dt || !dt.files || dt.files.length === 0) return;
    if (dt.files.length > 1) {
      setStatus("Only one file at a time, please.", "error");
      return;
    }
    setFile(dt.files[0]);
  });

  fileInput.addEventListener("change", function () {
    if (fileInput.files && fileInput.files[0]) {
      setFile(fileInput.files[0]);
    }
  });

  // --- Form submit ---
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!chosen) {
      setStatus("Pick a file first.", "error");
      return;
    }
    submitBtn.disabled = true;
    setStatus("Uploading…");

    var fd = new FormData();
    fd.append("file", chosen);
    if (titleInput.value) fd.append("title", titleInput.value);

    var xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);
    xhr.upload.onprogress = function (ev) {
      if (ev.lengthComputable) {
        var pct = Math.round((ev.loaded / ev.total) * 100);
        setStatus("Uploading… " + pct + "%");
      }
    };
    xhr.onload = function () {
      try {
        var body = JSON.parse(xhr.responseText || "{}");
      } catch (err) {
        setStatus("Upload failed: server returned a non-JSON response.", "error");
        submitBtn.disabled = false;
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        setStatus("Done. Redirecting…", "ok");
        var url = (body && body.redirect_url) || ("/sessions/" + (body.session && body.session.id));
        window.location.href = url;
        return;
      }
      var detail = (body && body.detail) ? (" " + body.detail) : "";
      setStatus("Upload failed (" + xhr.status + "): " + (body && body.error ? body.error : "unknown") + detail, "error");
      submitBtn.disabled = false;
    };
    xhr.onerror = function () {
      setStatus("Network error during upload.", "error");
      submitBtn.disabled = false;
    };
    xhr.send(fd);
  });
})();
