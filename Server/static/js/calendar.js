/* Calendar: the arrow keys step through the days. Everything else is
   plain links (subscriptions and imports: subscriptions.js). */
(function () {
  "use strict";

  /* ← and → step the chosen day (the same links as the day's arrows). */
  document.addEventListener("keydown", function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey || e.target.closest("input, textarea, select, [contenteditable]")) return;
    if (document.querySelector(".menu, .dialog-wrap") || !document.getElementById("search").hidden) return;
    var label = e.key === "ArrowLeft" ? "Previous day" : e.key === "ArrowRight" ? "Next day" : "";
    var link = label && document.querySelector('.cday__arrows [aria-label="' + label + '"]');
    if (link) { e.preventDefault(); location.href = link.href; }
  });
})();
