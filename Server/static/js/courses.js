/* Course settings: the palette pop-up, renaming in place, moving up and
   down, deleting and adding. Everything goes through the courses API. */
(function () {
  "use strict";
  var AW = window.AW;
  var IC = { up: "M6 15l6-6 6 6", down: "M6 9l6 6 6-6", trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" };

  function api(method, id, body) { return AW.api(method, "/api/v1/courses" + (id ? "/" + id : ""), body); }
  function reload(message) { if (message) AW.toastNext(message); location.reload(); }
  function failed(err) { AW.toast((err && err.message) || "Could not save that"); }

  /* --- Colour: a small pop-up with the twelve palette colours ------------- */
  var pop = null;
  function closePop() {
    if (!pop) return;
    pop.anchor.setAttribute("aria-expanded", "false");
    pop.el.remove(); pop = null;
    document.removeEventListener("mousedown", onDown, true);
  }
  function onDown(e) { if (pop && !pop.el.contains(e.target) && !pop.anchor.contains(e.target)) closePop(); }
  function openPalette(btn) {
    var row = btn.closest("[data-course]"), again = pop && pop.anchor === btn;
    closePop();
    if (again) return;
    var el = document.createElement("div");
    el.className = "palette"; el.setAttribute("role", "radiogroup"); el.setAttribute("aria-label", "Colour");
    for (var n = 1; n <= 12; n++) {
      el.insertAdjacentHTML("beforeend", '<button type="button" role="radio" class="cc-' + n + '" data-slot="' + n +
        '" aria-checked="' + (row.dataset.color === String(n)) + '" aria-label="Colour ' + n + '"></button>');
    }
    document.body.appendChild(el);
    var r = btn.getBoundingClientRect();
    el.style.left = Math.min(r.left, window.innerWidth - el.offsetWidth - 8) + "px";
    el.style.top = (r.bottom + 6 + el.offsetHeight > window.innerHeight ? r.top - el.offsetHeight - 6 : r.bottom + 6) + "px";
    btn.setAttribute("aria-expanded", "true");
    pop = { el: el, anchor: btn };
    document.addEventListener("mousedown", onDown, true);
    (el.querySelector('[aria-checked="true"]') || el.firstChild).focus();
    el.addEventListener("keydown", function (e) {
      var all = Array.prototype.slice.call(el.children), i = all.indexOf(document.activeElement);
      var step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 6, ArrowUp: -6 }[e.key];
      if (step) { e.preventDefault(); (all[(i + step + 12) % 12]).focus(); }
      if (e.key === "Escape") { closePop(); btn.focus(); }
    });
    el.addEventListener("click", function (e) {
      var b = e.target.closest("[data-slot]");
      if (!b) return;
      var slot = b.dataset.slot;
      closePop();
      api("PATCH", row.dataset.course, { color: slot }).then(function () {
        row.className = row.className.replace(/\bcc-\d+\b/, "cc-" + slot);
        row.dataset.color = slot;
        AW.toast("Colour changed");
      }).catch(failed);
    });
  }

  /* --- Name and code: saved when you leave the field or press Enter -------- */
  document.querySelectorAll("[data-field]").forEach(function (input) {
    var saved = input.value;
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      if (e.key === "Escape") { input.value = saved; input.blur(); }
    });
    input.addEventListener("change", function () {
      var row = input.closest("[data-course]"), value = input.value.trim(), body = {};
      if (input.dataset.field === "name" && !value) { input.value = saved; AW.toast("A course needs a name"); return; }
      body[input.dataset.field] = value || null;
      api("PATCH", row.dataset.course, body).then(function () {
        saved = value; input.value = value;
        if (input.dataset.field === "name") row.dataset.name = value;
        AW.toast("Saved");
      }).catch(function (err) { input.value = saved; failed(err); });
    });
  });

  /* --- Move up / down, Delete --------------------------------------------- */
  function reorder(row, dir) {
    var rows = Array.prototype.slice.call(document.querySelectorAll("[data-course]"));
    var i = rows.indexOf(row), j = i + dir;
    if (j < 0 || j >= rows.length) return;
    rows.splice(j, 0, rows.splice(i, 1)[0]);
    Promise.all(rows.map(function (r, k) { return api("PATCH", r.dataset.course, { sort_order: k * 10 }); }))
      .then(function () { reload(); }).catch(failed);
  }
  function remove(row) {
    AW.confirm({ title: "Delete this course?", name: row.dataset.name, text: "Its lectures stay, without a course." })
      .then(function (yes) {
        if (yes) api("DELETE", row.dataset.course).then(function () { reload("Course deleted"); }).catch(failed);
      });
  }
  document.addEventListener("click", function (e) {
    var sw = e.target.closest("[data-swatch]");
    if (sw) { openPalette(sw); return; }
    var dots = e.target.closest("[data-course-menu]");
    if (!dots) return;
    var row = dots.closest("[data-course]"), rows = document.querySelectorAll("[data-course]");
    var items = [];
    if (row !== rows[0]) items.push({ label: "Move up", icon: IC.up, onSelect: function () { reorder(row, -1); } });
    if (row !== rows[rows.length - 1]) items.push({ label: "Move down", icon: IC.down, onSelect: function () { reorder(row, 1); } });
    if (items.length) items.push({ sep: true });
    items.push({ label: "Delete", icon: IC.trash, danger: true, onSelect: function () { remove(row); } });
    AW.menu(dots, items, { title: row.dataset.name });
  });

  /* --- Add ------------------------------------------------------------------ */
  var add = document.querySelector("[data-course-add]");
  add.addEventListener("submit", function (e) {
    e.preventDefault();
    var name = add.elements["name"].value.trim(), code = add.elements["code"].value.trim();
    if (!name) return;
    api("POST", null, { name: name, code: code || null }).then(function () { reload("Course added"); }).catch(failed);
  });
})();
