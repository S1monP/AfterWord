/* To-dos and Deadlines: ticking (with Undo), the filters and the phone
   switch, quick add with the urgency pop-up, the actions menu, the Edit
   dialog and Delete (with Undo). Changes that move items between groups
   reload the page and keep the scroll position. */
(function () {
  "use strict";
  var AW = window.AW;
  var page = document.querySelector(".tpage");
  var PRIOS = [["high", "High"], ["medium", "Medium"], ["low", "Low"]];
  var IC = {
    pencil: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4", calendar: "M4 5h16v15H4zM4 10h16M9 3v4M15 3v4",
    swap: "M7 7h13l-3-3M17 17H4l3 3", flag: "M5 21V4h11l-2 4 2 4H5",
    play: "M8 5v14l11-7z", trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
  };
  var courses = [];
  try { courses = JSON.parse(document.getElementById("courses-data").textContent); } catch (e) {}

  /* --- Reload, keeping the scroll and the tab ------------------------------ */
  function reload(message) {
    try {
      sessionStorage.setItem("aw-todos-scroll", JSON.stringify({
        y: window.scrollY,
        lists: Array.prototype.map.call(document.querySelectorAll(".tlist"), function (l) { return l.scrollTop; }),
      }));
    } catch (e) {}
    if (message) AW.toastNext(message);
    location.reload();
  }
  try {
    var kept = JSON.parse(sessionStorage.getItem("aw-todos-scroll") || "null");
    sessionStorage.removeItem("aw-todos-scroll");
    if (kept) {
      document.querySelectorAll(".tlist").forEach(function (l, i) { l.scrollTop = kept.lists[i] || 0; });
      window.scrollTo(0, kept.y || 0);
    }
  } catch (e) {}

  /* --- Phone: To-dos | Deadlines (remembered; #deadlines opens it) ---------- */
  function showTab(kind) {
    page.dataset.tab = kind;
    document.querySelectorAll("[data-tab-btn]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.tabBtn === kind ? "true" : "false");
    });
    try { localStorage.setItem("aw-todos-tab", kind); } catch (e) {}
  }
  document.querySelectorAll("[data-tab-btn]").forEach(function (b) {
    b.addEventListener("click", function () { showTab(b.dataset.tabBtn); window.scrollTo(0, 0); });
  });
  var target = location.hash && document.querySelector(location.hash);
  if (location.hash === "#deadlines") showTab("deadline");
  else if (target && target.dataset.kind) showTab(target.dataset.kind);
  else { try { var savedTab = localStorage.getItem("aw-todos-tab"); if (savedTab) showTab(savedTab); } catch (e) {} }
  if (target && target.closest(".tdone")) target.closest(".tdone").open = true;

  /* --- Desktop: Open / Done / All ------------------------------------------- */
  document.querySelectorAll("[data-filter]").forEach(function (b) {
    b.addEventListener("click", function () {
      var panel = b.closest(".tpanel"), show = b.dataset.filter;
      panel.dataset.show = show;
      panel.querySelectorAll("[data-filter]").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      var done = panel.querySelector(".tdone");
      if (done) done.open = show !== "open";
    });
  });

  /* --- Ticking -------------------------------------------------------------- */
  function setStatus(row, done) {
    return AW.api("POST", "/api/v1/tasks/" + row.dataset.id + "/status", { status: done ? "done" : "open" });
  }
  document.addEventListener("change", function (e) {
    var cb = e.target.closest("[data-tick]");
    if (!cb) return;
    var row = cb.closest("[data-task]"), done = cb.checked;
    row.classList.toggle("is-done", done);
    setStatus(row, done).then(function () {
      AW.toast(done ? "Marked done" : "Marked not done", {
        undo: function () {
          cb.checked = !done; row.classList.toggle("is-done", !done);
          setStatus(row, !done).catch(function () { AW.toast("Could not undo that"); });
        },
      });
    }).catch(function () {
      cb.checked = !done; row.classList.toggle("is-done", !done);
      AW.toast("Could not save that");
    });
  });

  /* --- Urgency pop-up (the add bar's urgency button) ------------------------ */
  function prioTag(p) {
    var label = PRIOS.filter(function (x) { return x[0] === p; })[0][1];
    return '<span class="tag prio--' + p + '">' + label + "</span>";
  }
  document.querySelectorAll("[data-add-prio]").forEach(function (b) {
    b.addEventListener("click", function () {
      AW.menu(b, PRIOS.map(function (p) {
        return {
          label: p[1], labelCls: "tag prio--" + p[0], checked: b.dataset.addPrio === p[0],
          onSelect: function () {
            b.dataset.addPrio = p[0]; b.innerHTML = prioTag(p[0]);
            b.setAttribute("aria-label", "Urgency: " + p[1]);
          },
        };
      }), { title: "Urgency", pop: true });
    });
  });

  /* --- Quick add ------------------------------------------------------------ */
  function niceDate(iso) {
    var d = new Date(iso + "T00:00");
    return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }
  document.querySelectorAll(".tadd__date input").forEach(function (input) {
    var wrap = input.closest(".tadd__date"), text = wrap.querySelector("[data-date-text]"), empty = text.textContent;
    input.addEventListener("click", function () { try { input.showPicker(); } catch (e) {} });
    input.addEventListener("change", function () {
      wrap.classList.toggle("has-date", !!input.value);
      text.textContent = input.value ? niceDate(input.value) : empty;
    });
  });
  document.querySelectorAll("[data-add]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var kind = form.dataset.add, title = form.elements["title"].value.trim(), due = form.elements["due"].value;
      if (!title) return;
      if (kind === "deadline" && !due) {
        AW.toast("A deadline needs a date");
        try { form.elements["due"].showPicker(); } catch (err) { form.elements["due"].focus(); }
        return;
      }
      var prio = form.querySelector("[data-add-prio]");
      AW.api("POST", "/api/v1/tasks", {
        title: title, kind: kind, due_at: due || null,
        priority: prio ? prio.dataset.addPrio : "medium",
        course_id: form.elements["course_id"] ? form.elements["course_id"].value || null : null,
      }).then(function () {
        try { sessionStorage.setItem("aw-todos-add", kind); } catch (err) {}
        reload("Added");
      }).catch(function (err) { AW.toast(err.message || "Could not add that"); });
    });
  });
  try {
    var again = sessionStorage.getItem("aw-todos-add");
    sessionStorage.removeItem("aw-todos-add");
    if (again && !AW.isPhone()) document.getElementById("add-" + again).focus();
  } catch (e) {}

  /* --- The actions menu (three dots; on the phone, tapping the row) --------- */
  function patch(row, body, message) {
    return AW.api("PATCH", "/api/v1/tasks/" + row.dataset.id, body)
      .then(function () { reload(message); })
      .catch(function (err) { AW.toast(err.message || "Could not save that"); });
  }
  function openActions(row, anchor) {
    var d = row.dataset, isTask = d.kind === "task";
    var items = [
      { label: "Edit", icon: IC.pencil, onSelect: function () { edit(row); } },
      { label: d.due ? "Change the date" : "Add a date", icon: IC.calendar, onSelect: function () { edit(row, { focus: "due" }); } },
      {
        label: isTask ? "Make it a deadline" : "Make it a to-do", icon: IC.swap,
        onSelect: function () {
          if (isTask && !d.due) edit(row, { kind: "deadline", focus: "due" });
          else patch(row, { kind: isTask ? "deadline" : "task" }, isTask ? "Moved to deadlines" : "Moved to to-dos");
        },
      },
    ];
    if (isTask) {
      items.push({ sep: true });
      items.push({
        label: "Urgency", icon: IC.flag, end: d.prio.charAt(0).toUpperCase() + d.prio.slice(1),
        sub: PRIOS.map(function (p) {
          return { label: p[1], checked: d.prio === p[0], onSelect: function () { if (d.prio !== p[0]) patch(row, { priority: p[0] }); } };
        }),
      });
    }
    if (d.rec) items.push({ sep: true }, { label: "Open the recording there", icon: IC.play, onSelect: function () { location.href = d.rec; } });
    items.push({ sep: true }, { label: "Delete", icon: IC.trash, danger: true, onSelect: function () { remove(row); } });
    AW.menu(anchor, items, { title: AW.isPhone() ? d.title : "" });
  }
  document.addEventListener("click", function (e) {
    var dots = e.target.closest("[data-actions]");
    if (dots) { openActions(dots.closest("[data-task]"), dots); return; }
    var body = e.target.closest("[data-row-open]");
    if (body && AW.isPhone() && !e.target.closest("a")) openActions(body.closest("[data-task]"), body);
  });

  /* --- Delete, with Undo (puts it back as it was) --------------------------- */
  function remove(row) {
    var id = row.dataset.id;
    AW.api("GET", "/api/v1/tasks/" + id).then(function (data) {
      var t = data.task;
      return AW.api("DELETE", "/api/v1/tasks/" + id).then(function () {
        row.hidden = true;
        AW.toast("Deleted", {
          undo: function () {
            var copy = {};
            ["title", "kind", "body_md", "description", "priority", "due_at", "source", "source_ref",
             "source_offset_ms", "course_id", "pinned"].forEach(function (k) { if (t[k] !== undefined) copy[k] = t[k]; });
            AW.api("POST", "/api/v1/tasks", copy).then(function (made) {
              return t.status !== "open" ? AW.api("POST", "/api/v1/tasks/" + made.task.id + "/status", { status: t.status }) : null;
            }).then(function () { reload(); }).catch(function () { AW.toast("Could not undo that"); });
          },
        });
      });
    }).catch(function () { AW.toast("Could not delete that"); });
  }

  /* --- The Edit dialog ------------------------------------------------------ */
  function edit(row, opts) {
    opts = opts || {};
    var d = row.dataset, kind = opts.kind || d.kind, isTask = kind === "task";
    var wrap = document.createElement("div");
    wrap.className = "dialog-wrap";
    var courseOptions = '<option value="">No course</option>' + courses.map(function (c) {
      return '<option value="' + AW.esc(c.id) + '"' + (c.id === d.course ? " selected" : "") + ">" + AW.esc(c.name) + "</option>";
    }).join("");
    wrap.innerHTML =
      '<form class="dialog" role="dialog" aria-modal="true" aria-labelledby="tedit-t">' +
      '<h2 id="tedit-t">' + (opts.kind === "deadline" ? "Make it a deadline" : isTask ? "Edit to-do" : "Edit deadline") + "</h2>" +
      '<div class="tedit">' +
      '<label class="form-row"><span>Title</span><input class="field" name="title" maxlength="500" required></label>' +
      '<div class="tedit__two">' +
      '<label class="form-row"><span>' + (isTask ? "Date" : "Due date") + '</span><input class="field" type="date" name="due"' + (isTask ? "" : " required") + "></label>" +
      '<label class="form-row"><span>Time</span><input class="field" type="time" name="time"></label></div>' +
      (isTask ? '<fieldset class="form-row" style="border:0;margin:0;padding:0"><legend class="form-label">Urgency</legend><div class="seg">' +
        PRIOS.map(function (p) {
          return '<label><input type="radio" name="prio" value="' + p[0] + '"' + (d.prio === p[0] ? " checked" : "") + "><span>" + p[1] + "</span></label>";
        }).join("") + "</div></fieldset>" : "") +
      '<label class="form-row"><span>Course</span><select class="field" name="course">' + courseOptions + "</select></label>" +
      "</div>" +
      '<div class="dialog__actions"><button type="button" class="btn" data-no>Cancel</button>' +
      '<button type="submit" class="btn btn--fill">Save</button></div></form>';
    var form = wrap.querySelector("form");
    form.elements["title"].value = d.title; form.elements["due"].value = d.due; form.elements["time"].value = d.time;
    function close() { wrap.remove(); document.removeEventListener("keydown", onKey); }
    function onKey(e) { if (e.key === "Escape") close(); }
    wrap.addEventListener("mousedown", function (e) { if (e.target === wrap) close(); });
    wrap.querySelector("[data-no]").addEventListener("click", close);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var due = form.elements["due"].value ? form.elements["due"].value + (form.elements["time"].value ? "T" + form.elements["time"].value : "") : null;
      var body = { title: form.elements["title"].value.trim(), due_at: due, course_id: form.elements["course"].value || null };
      if (opts.kind) body.kind = opts.kind;
      if (isTask) body.priority = form.elements["prio"].value;
      AW.api("PATCH", "/api/v1/tasks/" + d.id, body)
        .then(function () { close(); reload("Saved"); })
        .catch(function (err) { AW.toast(err.message || "Could not save that"); });
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(wrap);
    (opts.focus === "due" ? form.elements["due"] : form.elements["title"]).focus();
  }
})();
