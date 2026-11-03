/* Search: the same results in Home's dropdown,
   the popup over any other page (rail magnifier or "/"), and the phone's
   full-screen search. Results come from /api/v1/search, grouped; each
   opens the item at its moment. */
(function () {
  "use strict";
  var AW = window.AW || {};
  var ICONS = {
    lecture: "M3 7l9-4 9 4-9 4zM7 9v5c0 1.5 2.5 3 5 3s5-1.5 5-3V9",
    meeting: "M8 12a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM16 12a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM3 19c0-3 2.5-5 5-5s5 2 5 5M11 19c0-3 2.5-5 5-5s5 2 5 5",
    note: "M6 3h9l4 4v14H6zM9 11h7M9 15h7",
    other: "M5 12h.01M12 12h.01M19 12h.01",
    todo: "M4 6l2 2 3-3M12 7h8M4 14l2 2 3-3M12 15h8",
    deadline: "M5 21V4h11l-2 4 2 4H5",
    event: "M4 5h16v15H4zM4 10h16M9 3v4M15 3v4",
  };
  var SHOWN = 3;   // per group before "Show N more"
  var HINT = "Search lectures, transcripts, notes, to-dos and the calendar. Every form of a word matches, in English and Russian.";

  function segs(parts) {
    return (parts || []).map(function (p) {
      return p[1] ? "<mark>" + AW.esc(p[0]) + "</mark>" : AW.esc(p[0]);
    }).join("");
  }

  /* One search box with its results list. */
  function Searcher(input, results, opts) {
    this.input = input; this.results = results; this.opts = opts || {};
    this.ctrl = null; this.timer = null; this.data = null; this.more = {}; this.active = 0;
    var self = this;
    input.addEventListener("input", function () { self.queue(); });
    input.addEventListener("keydown", function (e) { self.key(e); });
    if (input.form) input.form.addEventListener("submit", function (e) { e.preventDefault(); self.openActive(); });
    this.hint();
  }
  Searcher.prototype.hint = function () {
    this.results.innerHTML = this.opts.noHint ? "" : '<p class="sres__hint">' + HINT + "</p>";
    this.data = null;
    this.input.setAttribute("aria-expanded", "false");
    if (this.opts.onEmpty) this.opts.onEmpty();
  };
  Searcher.prototype.queue = function () {
    var self = this;
    clearTimeout(this.timer);
    this.timer = setTimeout(function () { self.run(); }, 140);
  };
  Searcher.prototype.run = function () {
    var q = this.input.value.trim(), self = this;
    if (this.ctrl) this.ctrl.abort();
    if (q.length < 2) { this.hint(); return; }
    this.ctrl = new AbortController();
    fetch("/api/v1/search?q=" + encodeURIComponent(q), { signal: this.ctrl.signal })
      .then(function (r) { return r.json(); })
      .then(function (d) { self.data = d; self.more = {}; self.active = 0; self.render(); })
      .catch(function (e) { if (e.name !== "AbortError") self.results.innerHTML = '<p class="sres__hint">Search is not available right now.</p>'; });
  };
  Searcher.prototype.render = function () {
    var d = this.data, self = this, html = "", n = 0;
    if (!d) return;
    if (!d.groups.length) {
      html = '<p class="sres__hint">Nothing found for “' + AW.esc(d.query) + '”. Try fewer words, or part of a word.</p>';
    }
    d.groups.forEach(function (g) {
      var all = self.more[g.type] ? g.items : g.items.slice(0, SHOWN);
      html += '<section class="sres__group"><h3 class="sres__label">' + AW.esc(g.label) + ' <span class="mono">' + g.total + "</span></h3>";
      all.forEach(function (it) {
        var id = "sres-" + n;
        html += '<div class="sres__item' + (n === self.active ? " is-active" : "") + '" id="' + id + '" role="option" aria-selected="' + (n === self.active) + '" data-n="' + n + '">' +
          '<a class="sres__main" href="' + AW.esc(it.url) + '">' +
          '<span class="sres__icon ' + AW.esc(it.cc || "cc-0") + '">' + AW.icon(ICONS[it.icon] || ICONS.other, 16) + "</span>" +
          '<span class="sres__body"><span class="sres__title">' + segs(it.title) + "</span>" +
          '<span class="sres__meta">' + AW.esc(it.meta) + "</span>" +
          (it.snippet ? '<span class="sres__snip">' + (it.at ? '<span class="sres__at">' + AW.esc(it.at) + "</span>" : "") + segs(it.snippet) + "</span>" : "") +
          "</span></a>";
        if (it.moments && it.moments.length) {
          html += '<div class="sres__moments">' + it.moments.map(function (m) {
            return '<a class="sres__moment" href="' + AW.esc(m.url) + '">' + AW.esc(m.label) + "</a>";
          }).join("") + (it.more ? '<span class="sres__moment">+' + it.more + "</span>" : "") + "</div>";
        }
        html += "</div>";
        n++;
      });
      if (g.items.length > all.length) {
        html += '<button type="button" class="sres__more" data-more="' + AW.esc(g.type) + '">Show ' + (g.items.length - all.length) + " more</button>";
      }
      html += "</section>";
    });
    if (d.groups.length && !AW.isPhone()) {
      html += '<div class="sres__foot"><span><span class="kbd">↑</span><span class="kbd">↓</span> move</span>' +
        '<span><span class="kbd">Enter</span> open</span><span><span class="kbd">Esc</span> close</span></div>';
    }
    this.count = n;
    this.results.innerHTML = html;
    this.input.setAttribute("aria-expanded", n ? "true" : "false");
    this.input.setAttribute("aria-activedescendant", n ? "sres-" + this.active : "");
    this.results.querySelectorAll("[data-more]").forEach(function (b) {
      b.addEventListener("click", function () { self.more[b.dataset.more] = true; self.render(); self.input.focus(); });
    });
    this.results.querySelectorAll(".sres__item").forEach(function (el) {
      el.addEventListener("mousemove", function () { self.setActive(+el.dataset.n, false); });
    });
    if (this.opts.onResults) this.opts.onResults();
  };
  Searcher.prototype.setActive = function (n, scroll) {
    if (!this.count) return;
    this.active = (n + this.count) % this.count;
    var self = this;
    this.results.querySelectorAll(".sres__item").forEach(function (el) {
      var on = +el.dataset.n === self.active;
      el.classList.toggle("is-active", on);
      el.setAttribute("aria-selected", on ? "true" : "false");
      if (on && scroll) el.scrollIntoView({ block: "nearest" });
    });
    this.input.setAttribute("aria-activedescendant", "sres-" + this.active);
  };
  Searcher.prototype.openActive = function () {
    var el = this.results.querySelector('.sres__item[data-n="' + this.active + '"] .sres__main');
    if (el) window.location.href = el.href;
  };
  Searcher.prototype.key = function (e) {
    if (e.key === "ArrowDown") { e.preventDefault(); this.setActive(this.active + 1, true); }
    else if (e.key === "ArrowUp") { e.preventDefault(); this.setActive(this.active - 1, true); }
    else if (e.key === "Escape" && this.opts.onEscape) { e.preventDefault(); this.opts.onEscape(); }
  };
  AW.Searcher = Searcher;

  /* The popup / full-screen search. */
  var overlay = document.getElementById("search");
  var popup = null;
  function openPopup() {
    if (!overlay) return;
    overlay.hidden = false;
    document.body.style.overflow = "hidden";
    var input = overlay.querySelector("[data-search-input]");
    if (!popup) {
      popup = new Searcher(input, overlay.querySelector("[data-search-results]"), { onEscape: closePopup });
    }
    var nav = document.getElementById("nav");
    if (nav) nav.classList.remove("is-open");
    input.focus(); input.select();
  }
  function closePopup() {
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    document.body.style.overflow = "";
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest("[data-open-search]")) { e.preventDefault(); openPopup(); return; }
    if (e.target.closest("[data-close-search]")) { closePopup(); return; }
    if (overlay && e.target === overlay) closePopup();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) return;
    e.preventDefault();
    var home = document.querySelector("[data-home-search]");
    if (home && !AW.isPhone()) { home.focus(); home.select(); } else openPopup();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePopup(); });
  AW.openSearch = openPopup;
  if (location.hash === "#search") openPopup();
})();
