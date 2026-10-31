/* Home: the schedule switch, Recent's tabs, to-dos (tick, add), the
   search box's dropdown and the clock. */
(function () {
  "use strict";
  var AW = window.AW;

  /* This week | Today (remembered per browser). */
  function showSched(view) {
    document.querySelectorAll("[data-sched]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.sched === view ? "true" : "false");
    });
    document.querySelectorAll("[data-sched-view]").forEach(function (v) { v.hidden = v.dataset.schedView !== view; });
    try { localStorage.setItem("aw-home-sched", view); } catch (e) {}
  }
  document.querySelectorAll("[data-sched]").forEach(function (b) {
    b.addEventListener("click", function () { showSched(b.dataset.sched); });
  });
  try { var saved = localStorage.getItem("aw-home-sched"); if (saved === "day") showSched("day"); } catch (e) {}

  /* Recent: All / Lectures / Meetings / Notes. */
  document.querySelectorAll("[data-recent]").forEach(function (b) {
    b.addEventListener("click", function () {
      var kind = b.dataset.recent;
      document.querySelectorAll("[data-recent]").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      document.querySelectorAll(".ritem").forEach(function (it) { it.hidden = kind !== "all" && it.dataset.kind !== kind; });
    });
  });

  /* To-dos: ticking marks done (with Undo); the same to-do may be on the page twice (desktop, phone). */
  function setDone(id, done) {
    document.querySelectorAll('[data-todo="' + id + '"]').forEach(function (cb) {
      cb.checked = done;
      var row = cb.closest(".trow, .card");
      if (row) row.classList.toggle("is-done", done);
    });
  }
  document.addEventListener("change", function (e) {
    var cb = e.target.closest("[data-todo]");
    if (!cb) return;
    var id = cb.dataset.todo, done = cb.checked;
    setDone(id, done);
    AW.api("POST", "/api/v1/tasks/" + id + "/status", { status: done ? "done" : "open" })
      .then(function () {
        if (done) AW.toast("Done", { undo: function () {
          setDone(id, false);
          AW.api("POST", "/api/v1/tasks/" + id + "/status", { status: "open" });
        } });
      })
      .catch(function () { setDone(id, !done); AW.toast("Could not save that"); });
  });

  var addForm = document.querySelector("[data-todo-add]");
  if (addForm) addForm.addEventListener("submit", function (e) {
    e.preventDefault();
    var input = addForm.querySelector("input"), title = input.value.trim();
    if (!title) return;
    input.disabled = true;
    AW.api("POST", "/api/v1/tasks", { title: title, kind: "task", priority: "medium" })
      .then(function (data) {
        var t = data.task, list = document.querySelector("[data-todo-list]");
        var li = document.createElement("li");
        li.className = "trow";
        li.innerHTML = '<label class="check"><input type="checkbox" data-todo="' + AW.esc(t.id) + '" aria-label="Done: ' + AW.esc(t.title) + '"></label>' +
          '<span class="trow__body"><span class="trow__title">' + AW.esc(t.title) + '</span>' +
          '<span class="trow__meta"><span class="tag prio--medium">Medium</span></span></span>';
        list.insertBefore(li, list.firstChild);
        var none = document.querySelector("[data-todo-none]");
        if (none) none.remove();
        input.value = "";
      })
      .catch(function (err) { AW.toast(err.message || "Could not add the to-do"); })
      .then(function () { input.disabled = false; input.focus(); });
  });

  /* The search box: results drop down under it. */
  var box = document.querySelector("[data-home-search]");
  var drop = document.getElementById("home-search-results");
  if (box && drop && AW.Searcher) {
    var hide = function () { drop.hidden = true; box.setAttribute("aria-expanded", "false"); };
    new AW.Searcher(box, drop, {
      noHint: true,
      onResults: function () { drop.hidden = false; },
      onEmpty: hide,
      onEscape: function () { hide(); box.blur(); },
    });
    box.addEventListener("focus", function () { if (drop.innerHTML.trim() && box.value.trim().length > 1) drop.hidden = false; });
    document.addEventListener("mousedown", function (e) { if (!e.target.closest("[data-home-search-form]")) hide(); });
    var q = new URLSearchParams(location.search).get("q");
    if (q) { box.value = q; box.dispatchEvent(new Event("input")); box.focus(); }
  }

  /* The clock in the top bar. */
  var clock = document.querySelector("[data-clock]");
  if (clock) {
    var tick = function () {
      var d = new Date();
      var day = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()];
      var mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getMonth()];
      clock.textContent = day + " " + String(d.getDate()).padStart(2, "0") + " " + mon + " · " +
        String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    };
    setInterval(tick, 30000);
  }
})();
