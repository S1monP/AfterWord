/* Calendar: Import .ics, and the arrow keys step through the days.
   Everything else is plain links. */
(function () {
  "use strict";
  var AW = window.AW;

  /* Import .ics: pick a file, send it, say how many came in, reload. */
  var file = document.querySelector("[data-ics-file]");
  document.querySelectorAll("[data-ics-import]").forEach(function (b) {
    b.addEventListener("click", function () { file.value = ""; file.click(); });
  });
  file.addEventListener("change", function () {
    if (!file.files.length) return;
    var data = new FormData();
    data.append("file", file.files[0]);
    AW.toast("Importing " + file.files[0].name + "…");
    fetch("/api/v1/events/import", { method: "POST", body: data })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        var n = res.j && res.j.imported;
        if (!res.ok) { AW.toast(res.j.detail || res.j.error || "Could not import that file"); return; }
        if (!n) { AW.toast("No events found in that file"); return; }
        AW.toastNext("Imported " + n + " event" + (n === 1 ? "" : "s"));
        location.reload();
      })
      .catch(function () { AW.toast("Could not import that file"); });
  });

  /* ← and → step the chosen day (the same links as the day's arrows). */
  document.addEventListener("keydown", function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey || e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (document.querySelector(".menu, .dialog-wrap") || !document.getElementById("search").hidden) return;
    var label = e.key === "ArrowLeft" ? "Previous day" : e.key === "ArrowRight" ? "Next day" : "";
    var link = label && document.querySelector('.cday__arrows [aria-label="' + label + '"]');
    if (link) { e.preventDefault(); location.href = link.href; }
  });
})();
