// Meetings two-pane behaviour: soft-nav on meeting click.
//
// Same pattern as lectures.js but the sidebar is a flat list
// (no expand/collapse, no courses). The X-Soft-Nav header asks the
// server to render only the right pane; we swap innerHTML and
// update the URL via history.replaceState.

(function () {
  var sidebar = document.querySelector(".lr-twopane__sidebar");
  var main = document.getElementById("lr-twopane-main");
  if (!sidebar || !main) return;

  // Highlight the initially-selected link (the server already
  // marks it with .is-active via the inline style, but we
  // re-apply so soft-nav swaps stay consistent).
  Array.prototype.forEach.call(
    sidebar.querySelectorAll("[data-session-link]"),
    function (link) {
      link.addEventListener("click", function (e) {
        e.preventDefault();
        var href = link.getAttribute("href");
        var url = new URL(href, window.location.origin);
        var sid = url.searchParams.get("session");
        if (!sid) { window.location.href = href; return; }

        Array.prototype.forEach.call(
          sidebar.querySelectorAll(".lr-course__lecture"),
          function (a) { a.classList.remove("is-active"); }
        );
        link.classList.add("is-active");

        softLoad(href, sid);
      });
    }
  );

  function softLoad(href, sid) {
    fetch(href, { headers: { "X-Soft-Nav": "1" } })
      .then(function (r) {
        if (!r.ok) throw new Error("fetch failed: " + r.status);
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var newMain = doc.getElementById("lr-twopane-main");
        if (!newMain) throw new Error("missing main");
        main.innerHTML = newMain.innerHTML;
        try { history.replaceState(null, "", href); }
        catch (e) { /* ignore */ }
        main.dispatchEvent(new CustomEvent("lr:soft-nav-loaded", {
          detail: { sessionId: sid },
        }));
      })
      .catch(function () {
        window.location.href = href;
      });
  }
})();