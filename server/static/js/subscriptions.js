/* Calendar subscriptions: add a link, update now, edit, remove, create
   courses for its modules; and one-off .ics imports. */
(function () {
  "use strict";
  var AW = window.AW;
  var KINDS = [["lecture", "Lectures"], ["meeting", "Meetings"], ["other", "Other"]];
  var IC = { pencil: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4", trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" };
  var courses = [];
  try { courses = JSON.parse(document.getElementById("courses-data").textContent); } catch (e) {}

  function reload(message) { if (message) AW.toastNext(message); location.reload(); }
  function failed(err) { AW.toast((err && err.message) || "Something went wrong"); }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  /* --- Add a link --------------------------------------------------------- */
  var add = document.querySelector("[data-sub-add]");
  var state = add.querySelector("[data-state]");
  add.addEventListener("submit", function (e) {
    e.preventDefault();
    var go = add.querySelector("[type=submit]");
    go.disabled = true; state.classList.remove("is-bad"); state.textContent = "Checking the link…";
    AW.api("POST", "/api/v1/subscriptions", {
      url: add.elements["url"].value.trim(), name: add.elements["name"].value.trim(),
      kind: add.elements["kind"].value, course_id: add.elements["course_id"].value || null,
    }).then(function (d) {
      reload("Subscribed: " + plural(d.result.added, "event") + " added");
    }).catch(function (err) {
      go.disabled = false; state.classList.add("is-bad");
      state.textContent = err.message || "That link didn't work";
    });
  });

  /* --- Each subscription ---------------------------------------------------- */
  function syncNow(row, btn) {
    btn.disabled = true; btn.classList.add("is-spinning");
    AW.api("POST", "/api/v1/subscriptions/" + row.dataset.sub + "/sync")
      .then(function (d) { reload("Updated: " + d.summary); })
      .catch(function (err) { reload(err.message || "Couldn't update"); });
  }
  function edit(row) {
    var d = row.dataset, wrap = document.createElement("div");
    wrap.className = "dialog-wrap";
    wrap.innerHTML =
      '<form class="dialog" role="dialog" aria-modal="true" aria-labelledby="sedit-t">' +
      '<h2 id="sedit-t">Edit subscription</h2><div class="sedit">' +
      '<label class="form-row"><span>Name</span><input class="field" name="name" maxlength="100" required></label>' +
      '<fieldset class="form-row" style="border:0;margin:0;padding:0"><legend class="form-label">These events are</legend><div class="seg">' +
      KINDS.map(function (k) {
        return '<label><input type="radio" name="kind" value="' + k[0] + '"' + (d.kind === k[0] ? " checked" : "") + "><span>" + k[1] + "</span></label>";
      }).join("") + "</div></fieldset>" +
      '<label class="form-row"><span>Course</span><select class="field" name="course"><option value="">Match each event to a course by name</option>' +
      courses.map(function (c) {
        return '<option value="' + AW.esc(c.id) + '"' + (c.id === d.course ? " selected" : "") + ">All " + AW.esc(c.name) + "</option>";
      }).join("") + "</select></label>" +
      '<p class="sedit__hint">A new kind or course applies to all ' + plural(+d.count, "event") + ".</p></div>" +
      '<div class="dialog__actions"><button type="button" class="btn" data-no>Cancel</button>' +
      '<button type="submit" class="btn btn--fill">Save</button></div></form>';
    var form = wrap.querySelector("form");
    form.elements["name"].value = d.name;
    function close() { wrap.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    wrap.addEventListener("mousedown", function (e) { if (e.target === wrap) close(); });
    wrap.querySelector("[data-no]").addEventListener("click", close);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      AW.api("PATCH", "/api/v1/subscriptions/" + d.sub, {
        name: form.elements["name"].value.trim(), kind: form.elements["kind"].value,
        course_id: form.elements["course"].value || null,
      }).then(function () { close(); reload("Saved"); }).catch(failed);
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(wrap);
    form.elements["name"].focus();
  }
  function remove(row) {
    AW.confirm({
      title: "Remove this subscription?", name: row.dataset.name, ok: "Remove",
      text: "Its " + plural(+row.dataset.count, "event") + " leave your calendar. Recordings made in them stay.",
    }).then(function (yes) {
      if (yes) AW.api("DELETE", "/api/v1/subscriptions/" + row.dataset.sub).then(function () { reload("Subscription removed"); }).catch(failed);
    });
  }
  document.addEventListener("click", function (e) {
    var row = e.target.closest("[data-sub]");
    if (!row) return;
    var sync = e.target.closest("[data-sync]");
    if (sync) { syncNow(row, sync); return; }
    var dots = e.target.closest("[data-sub-menu]");
    if (dots) {
      AW.menu(dots, [
        { label: "Edit", icon: IC.pencil, onSelect: function () { edit(row); } },
        { sep: true },
        { label: "Remove", icon: IC.trash, danger: true, onSelect: function () { remove(row); } },
      ], { title: row.dataset.name });
      return;
    }
    var make = e.target.closest("[data-make-courses]");
    if (make) {
      make.disabled = true;
      AW.api("POST", "/api/v1/subscriptions/" + row.dataset.sub + "/courses")
        .then(function (d) { reload(plural(d.created, "course") + " created"); }).catch(failed);
    }
  });

  /* --- One-off import ---------------------------------------------------------- */
  var imp = document.querySelector("[data-import]");
  var file = imp.querySelector("[data-import-file]"), impState = imp.querySelector("[data-import-state]");
  imp.querySelector("[data-import-pick]").addEventListener("click", function () { file.value = ""; file.click(); });
  file.addEventListener("change", function () {
    if (!file.files.length) return;
    var data = new FormData();
    data.append("file", file.files[0]);
    data.append("kind", imp.elements["import_kind"].value);
    impState.classList.remove("is-bad"); impState.textContent = "Importing " + file.files[0].name + "…";
    fetch("/api/v1/events/import", { method: "POST", body: data })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { impState.classList.add("is-bad"); impState.textContent = res.j.detail || "That file couldn't be imported"; return; }
        reload(res.j.imported ? "Imported: " + res.j.summary : "No events found in that file");
      })
      .catch(function () { impState.classList.add("is-bad"); impState.textContent = "That file couldn't be imported"; });
  });
})();
