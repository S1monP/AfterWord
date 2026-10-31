/* The app shell: navigation, theme and colour scheme, and small shared
   helpers every page uses (window.AW):

     AW.toast(text, {undo})            short message at the bottom
     AW.menu(anchor, items, {title})   actions menu: pop-up on desktop,
                                       sheet from the bottom on the phone
     AW.confirm({title, name, text, ok}) -> Promise<boolean>
     AW.api(method, url, body)         JSON fetch that throws on errors
     AW.isPhone()                      the phone layout is active
*/
(function () {
  "use strict";
  var root = document.documentElement;
  var AW = (window.AW = window.AW || {});

  AW.isPhone = function () { return window.matchMedia("(max-width: 899px)").matches; };

  AW.api = function (method, url, body) {
    return fetch(url, {
      method: method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var err = new Error(data.detail || data.error || ("HTTP " + r.status));
          err.status = r.status; err.data = data;
          throw err;
        }
        return data;
      });
    });
  };

  /* --- Icons used by scripts (same paths as templates/_icons.html) --- */
  var PATHS = {
    check: "M5 12l5 5 9-10", right: "M9 6l6 6-6 6", back: "M15 5l-7 7 7 7",
  };
  AW.icon = function (name, size) {
    size = size || 18;
    return '<svg class="ic" aria-hidden="true" width="' + size + '" height="' + size +
      '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="' +
      (PATHS[name] || name) + '"/></svg>';
  };
  AW.esc = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  /* --- Toast ---------------------------------------------------------- */
  var toastEl = null, toastTimer = null;
  AW.toast = function (text, opts) {
    opts = opts || {};
    if (toastEl) toastEl.remove();
    clearTimeout(toastTimer);
    toastEl = document.createElement("div");
    toastEl.className = "toast";
    toastEl.setAttribute("role", "status");
    toastEl.innerHTML = "<span></span>";
    toastEl.firstChild.textContent = text;
    if (opts.undo) {
      var b = document.createElement("button");
      b.type = "button"; b.textContent = "Undo";
      b.addEventListener("click", function () { dismiss(); opts.undo(); });
      toastEl.appendChild(b);
    }
    document.body.appendChild(toastEl);
    function dismiss() { if (toastEl) { toastEl.remove(); toastEl = null; } }
    toastTimer = setTimeout(dismiss, opts.undo ? 5000 : 2800);
  };

  /* --- Pop-up menus and sheets ----------------------------------------- */
  var openMenu = null;
  function closeMenu() {
    if (!openMenu) return;
    var m = openMenu; openMenu = null;
    m.el.remove();
    if (m.scrim) m.scrim.remove();
    if (m.anchor) { m.anchor.setAttribute("aria-expanded", "false"); m.anchor.focus({ preventScroll: true }); }
    document.removeEventListener("mousedown", onDocDown, true);
  }
  function onDocDown(e) {
    if (openMenu && !openMenu.el.contains(e.target) && e.target !== openMenu.anchor && !openMenu.anchor.contains(e.target)) closeMenu();
  }
  function place(el, anchor) {
    if (AW.isPhone()) return;
    var r = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
    var left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
    var top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    el.style.left = left + "px"; el.style.top = top + "px";
  }
  function renderItems(el, items, title, anchor, stack) {
    var html = title ? '<div class="menu__title">' + AW.esc(title) + "</div>" : "";
    if (stack.length) html += '<button type="button" class="menu__item menu__back-item" data-back>' + AW.icon("back") + "<span>" + AW.esc(stack[stack.length - 1].label) + "</span></button><div class=\"menu__sep\"></div>";
    items.forEach(function (it, i) {
      if (it.sep) { html += '<div class="menu__sep" role="separator"></div>'; return; }
      var role = it.checked !== undefined ? "menuitemradio" : "menuitem";
      var end = it.end || "";
      if (it.checked) end = AW.icon("check");
      else if (it.sub) end = AW.esc(it.end || "") + AW.icon("right", 16);
      html += '<button type="button" class="menu__item' + (it.danger ? " menu__item--danger" : "") + '" role="' + role + '" data-i="' + i + '"' +
        (it.checked !== undefined ? ' aria-checked="' + (it.checked ? "true" : "false") + '"' : "") + ">" +
        (it.swatch ? '<span class="sq ' + it.swatch + '" style="width:10px;height:10px"></span>' : (it.icon ? AW.icon(it.icon) : "")) +
        "<span>" + AW.esc(it.label) + "</span>" + (end ? '<span class="end">' + end + "</span>" : "") + "</button>";
    });
    el.innerHTML = html;
    el.querySelectorAll("[data-i]").forEach(function (b) {
      b.addEventListener("click", function () {
        var it = items[+b.dataset.i];
        if (it.sub) { stack.push({ items: items, label: it.label }); renderItems(el, it.sub, title, anchor, stack); place(el, anchor); focusFirst(el); return; }
        closeMenu();
        if (it.onSelect) it.onSelect();
      });
    });
    var back = el.querySelector("[data-back]");
    if (back) back.addEventListener("click", function () { var prev = stack.pop(); renderItems(el, prev.items, title, anchor, stack); place(el, anchor); focusFirst(el); });
  }
  function focusFirst(el) { var f = el.querySelector(".menu__item"); if (f) f.focus(); }
  AW.menu = function (anchor, items, opts) {
    opts = opts || {};
    var again = openMenu && openMenu.anchor === anchor;
    closeMenu();
    if (again) return;
    var el = document.createElement("div");
    el.className = "menu"; el.setAttribute("role", "menu");
    var scrim = null;
    if (AW.isPhone()) {
      scrim = document.createElement("div"); scrim.className = "menu-scrim";
      scrim.addEventListener("click", closeMenu);
      document.body.appendChild(scrim);
    }
    renderItems(el, items, opts.title, anchor, []);
    document.body.appendChild(el);
    place(el, anchor);
    anchor.setAttribute("aria-expanded", "true");
    openMenu = { el: el, anchor: anchor, scrim: scrim };
    document.addEventListener("mousedown", onDocDown, true);
    el.addEventListener("keydown", function (e) {
      var all = Array.prototype.slice.call(el.querySelectorAll(".menu__item"));
      var i = all.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); (all[i + 1] || all[0]).focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); (all[i - 1] || all[all.length - 1]).focus(); }
    });
    focusFirst(el);
  };

  /* --- Confirm dialog (destructive actions only) ------------------------ */
  AW.confirm = function (o) {
    return new Promise(function (resolve) {
      var wrap = document.createElement("div");
      wrap.className = "dialog-wrap";
      wrap.innerHTML = '<div class="dialog" role="alertdialog" aria-modal="true" aria-labelledby="dlg-t">' +
        '<h2 id="dlg-t"></h2>' + (o.name ? '<p class="dialog__name"></p>' : "") + "<p class=\"dialog__text\"></p>" +
        '<div class="dialog__actions"><button type="button" class="btn" data-no>Cancel</button>' +
        '<button type="button" class="btn btn--danger" data-yes></button></div></div>';
      wrap.querySelector("h2").textContent = o.title;
      if (o.name) wrap.querySelector(".dialog__name").textContent = "“" + o.name + "”";
      wrap.querySelector(".dialog__text").textContent = o.text || "";
      wrap.querySelector("[data-yes]").textContent = o.ok || "Delete";
      function done(v) { wrap.remove(); document.removeEventListener("keydown", onKey); resolve(v); }
      function onKey(e) { if (e.key === "Escape") done(false); }
      wrap.addEventListener("click", function (e) { if (e.target === wrap) done(false); });
      wrap.querySelector("[data-no]").addEventListener("click", function () { done(false); });
      wrap.querySelector("[data-yes]").addEventListener("click", function () { done(true); });
      document.addEventListener("keydown", onKey);
      document.body.appendChild(wrap);
      wrap.querySelector("[data-no]").focus();
    });
  };

  /* --- Navigation ---------------------------------------------------------- */
  var nav = document.getElementById("nav");
  function setNavOpen(open) {
    if (!nav) return;
    nav.classList.toggle("is-open", open);
    document.body.style.overflow = open ? "hidden" : "";
    if (open) { var f = nav.querySelector("[data-close-nav]"); if (f) f.focus(); }
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-open-nav]");
    if (t) { setNavOpen(true); return; }
    if (e.target.closest("[data-close-nav]")) { setNavOpen(false); return; }
    if (nav && nav.classList.contains("is-open") && e.target.closest(".nav a")) setNavOpen(false);
  });

  // Pin the rail open (remembered per browser).
  function syncPin() {
    var open = root.dataset.rail === "open";
    document.querySelectorAll("[data-pin-rail]").forEach(function (b) {
      b.querySelector(".nav__label").textContent = open ? "Collapse" : "Keep open";
      b.querySelector(".nav__tip").textContent = open ? "Collapse the sidebar" : "Keep the sidebar open";
      b.querySelector("path").setAttribute("d", open ? "M15 6l-6 6 6 6" : "M9 6l6 6-6 6");
    });
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest("[data-pin-rail]")) return;
    var open = root.dataset.rail !== "open";
    if (open) root.dataset.rail = "open"; else delete root.dataset.rail;
    try { localStorage.setItem("aw-rail", open ? "open" : ""); } catch (err) {}
    syncPin();
  });
  syncPin();

  /* --- Theme and colour scheme (stored by the server) ----------------------- */
  var NAMES = { violet: "Violet, refined", honey: "Honey amber", wine: "Wine & Walnut", copper: "Warm Copper" };
  function syncSettings() {
    document.querySelectorAll("[data-set-theme]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.setTheme === root.dataset.theme ? "true" : "false");
    });
    document.querySelectorAll("[data-set-scheme]").forEach(function (b) {
      b.setAttribute("aria-pressed", b.dataset.setScheme === root.dataset.scheme ? "true" : "false");
    });
    document.querySelectorAll("[data-scheme-name]").forEach(function (s) { s.textContent = NAMES[root.dataset.scheme] || ""; });
  }
  function save(key, value) {
    root.dataset[key] = value;
    syncSettings();
    var body = {}; body[key] = value;
    AW.api("PATCH", "/api/v1/settings", body).catch(function () { AW.toast("Could not save the setting"); });
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-set-theme]");
    if (t) { save("theme", t.dataset.setTheme); return; }
    var s = e.target.closest("[data-set-scheme]");
    if (s) save("scheme", s.dataset.setScheme);
  });

  // The rail's sun button opens the theme and colours pop-up.
  var themePop = document.getElementById("theme-pop");
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-theme-menu]");
    if (b && themePop) {
      var show = themePop.hidden;
      themePop.hidden = !show;
      b.setAttribute("aria-expanded", show ? "true" : "false");
      if (show) {
        var r = b.getBoundingClientRect();
        themePop.style.left = (r.right + 8) + "px";
        themePop.style.top = Math.max(8, r.bottom - themePop.offsetHeight) + "px";
      }
      return;
    }
    if (themePop && !themePop.hidden && !themePop.contains(e.target)) {
      themePop.hidden = true;
      var sun = document.querySelector("[data-theme-menu]");
      if (sun) sun.setAttribute("aria-expanded", "false");
    }
  });

  /* --- Keys ------------------------------------------------------------------ */
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (openMenu) { closeMenu(); return; }
    if (themePop && !themePop.hidden) { themePop.hidden = true; return; }
    if (nav && nav.classList.contains("is-open")) setNavOpen(false);
  });
  window.addEventListener("resize", function () { if (openMenu) closeMenu(); });

  // #menu in the address opens the menu (phone).
  if (location.hash === "#menu") setNavOpen(true);
})();
