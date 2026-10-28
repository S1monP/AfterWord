// Lectures two-pane behaviour.
//
// Click a course head to toggle its lectures (expand/collapse).
// Click a lecture -> soft-navigate to /lectures?session=<id>: the
// server re-renders the page with the right pane populated, then we
// swap just the right pane's innerHTML (so the sidebar stays put
// and we don't lose scroll position). The URL is updated via
// history.replaceState so the back button still works.

(function () {
  var sidebar = document.querySelector(".lr-twopane__sidebar");
  var main = document.getElementById("lr-twopane-main");
  if (!sidebar || !main) return;

  // Open the course that contains the selected session on initial
  // load. The server already marks them via data-has-selection.
  Array.prototype.forEach.call(
    sidebar.querySelectorAll(".lr-course[data-has-selection='1']"),
    function (course) {
      course.classList.add("is-open");
    }
  );

  // Sidebar: toggle on course-head click.
  Array.prototype.forEach.call(
    sidebar.querySelectorAll("[data-toggle-course]"),
    function (head) {
      head.addEventListener("click", function () {
        head.parentNode.classList.toggle("is-open");
      });
    }
  );

  // Soft-nav on lecture click. Intercept clicks on .lr-course__lecture
  // so we can fetch the rendered right pane instead of doing a full
  // page reload. Falls back to a full navigation on fetch failure.
  Array.prototype.forEach.call(
    sidebar.querySelectorAll("[data-session-link]"),
    function (link) {
      link.addEventListener("click", function (e) {
        e.preventDefault();
        var href = link.getAttribute("href");
        var url = new URL(href, window.location.origin);
        var sid = url.searchParams.get("session");
        if (!sid) { window.location.href = href; return; }

        // Highlight immediately for snappiness.
        Array.prototype.forEach.call(
          sidebar.querySelectorAll(".lr-course__lecture"),
          function (a) { a.classList.remove("is-active"); }
        );
        link.classList.add("is-active");
        // Make sure the parent course is open so the active link
        // is visible (handles case where a new course's lectures
        // are clicked after a session change).
        var course = link.closest(".lr-course");
        if (course) course.classList.add("is-open");

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
        // Parse the response and extract the right pane.
        var doc = new DOMParser().parseFromString(html, "text/html");
        var newMain = doc.getElementById("lr-twopane-main");
        if (!newMain) throw new Error("missing main");
        main.innerHTML = newMain.innerHTML;
        // Update the URL without reloading.
        try {
          history.replaceState(null, "", href);
        } catch (e) { /* ignore */ }
        // Rebind the audio element's seek support (the same script
          // already loaded for the page-level audio; just in case
          // the loaded markup needs to re-init, dispatch a custom
          // event the user script could listen for).
        main.dispatchEvent(new CustomEvent("lr:soft-nav-loaded", {
          detail: { sessionId: sid },
        }));
      })
      .catch(function () {
        // On failure, fall back to a full navigation.
        window.location.href = href;
      });
  }
})();