// The dashboard's search box: matches drop down as you type
// (web/api_search.py), grouped; a click or Enter opens one. "/" anywhere
// on the page jumps to the box.
(function () {
  "use strict";

  var form = document.getElementById("lr-search");
  if (!form) return;
  var input = form.querySelector("input[name=q]");
  var box = document.getElementById("lr-search-results");
  var SHOWN = 4;          // items per group before "Show more"
  var timer = null;
  var controller = null;
  var last = null;        // the response on screen
  var expanded = {};      // group type -> showing all
  var active = -1;        // keyboard position in options()

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // [[text, matched], ...] -> text with the matched words in <mark>.
  function marked(parent, segments) {
    segments.forEach(function (seg) {
      parent.appendChild(seg[1] ? el("mark", null, seg[0]) : document.createTextNode(seg[0]));
    });
  }

  function show(on) {
    box.hidden = !on;
    input.setAttribute("aria-expanded", on ? "true" : "false");
    if (!on) input.removeAttribute("aria-activedescendant");
  }

  function renderItem(item, n) {
    var row = el("div", "lr-sr__item");
    var a = el("a", "lr-sr__main");
    a.href = item.url;
    a.id = "lr-sr-" + n;
    a.setAttribute("role", "option");
    var title = el("div", "lr-sr__title");
    marked(title, item.title);
    a.appendChild(title);
    if (item.meta) a.appendChild(el("div", "lr-sr__meta", item.meta));
    if (item.snippet) {
      var snippet = el("div", "lr-sr__snippet");
      if (item.at) snippet.appendChild(el("span", "lr-sr__at", item.at));
      marked(snippet, item.snippet);
      a.appendChild(snippet);
    }
    row.appendChild(a);
    if (item.moments && item.moments.length) {
      var moments = el("div", "lr-sr__moments");
      item.moments.forEach(function (m) {
        var chip = el("a", "lr-sr__moment", m.label);
        chip.href = m.url;
        chip.title = "Open at " + m.label;
        moments.appendChild(chip);
      });
      if (item.more) moments.appendChild(el("span", "lr-sr__moment lr-sr__moment--more", "+" + item.more));
      row.appendChild(moments);
    }
    return row;
  }

  function render(data) {
    last = data;
    active = -1;
    box.textContent = "";
    var n = 0;
    if (!data.groups.length) {
      box.appendChild(el("div", "lr-sr__empty",
        "Nothing found for “" + data.query.trim() + "”."));
    }
    data.groups.forEach(function (group) {
      var section = el("div", "lr-sr__group");
      section.appendChild(el("div", "lr-sr__label", group.label + " · " + group.total));
      var all = expanded[group.type];
      (all ? group.items : group.items.slice(0, SHOWN)).forEach(function (item) {
        section.appendChild(renderItem(item, n++));
      });
      if (!all && group.total > SHOWN) {
        var more = el("button", "lr-sr__more", "Show " + (group.total - SHOWN) + " more");
        more.type = "button";
        more.setAttribute("data-group", group.type);
        section.appendChild(more);
      } else if (all && group.total > group.items.length) {
        section.appendChild(el("div", "lr-sr__note",
          "Showing " + group.items.length + " of " + group.total +
          ". Add a word to narrow it down."));
      }
      box.appendChild(section);
    });
    show(true);
  }

  function run() {
    var q = input.value;
    if (controller) controller.abort();
    if (q.trim().length < 2) {
      last = null;
      show(false);
      return;
    }
    controller = new AbortController();
    fetch("/api/v1/search?q=" + encodeURIComponent(q),
          { credentials: "same-origin", signal: controller.signal })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (data.query === input.value) render(data);
      })
      .catch(function (e) {
        if (e.name === "AbortError") return;
        last = null;
        box.textContent = "";
        box.appendChild(el("div", "lr-sr__empty", "Search failed (" + e.message + ")."));
        show(true);
      });
  }

  // Keyboard: arrows move through the results and "Show more" buttons.
  function options() {
    return box.hidden ? [] : Array.prototype.slice.call(
      box.querySelectorAll(".lr-sr__main, .lr-sr__more"));
  }

  function setActive(i) {
    var opts = options();
    if (!opts.length) return;
    active = (i + opts.length) % opts.length;
    opts.forEach(function (o, j) {
      (o.closest(".lr-sr__item") || o).classList.toggle("is-active", j === active);
    });
    opts[active].scrollIntoView({ block: "nearest" });
    if (opts[active].id) input.setAttribute("aria-activedescendant", opts[active].id);
  }

  input.addEventListener("input", function () {
    expanded = {};
    clearTimeout(timer);
    timer = setTimeout(run, 150);
  });

  input.addEventListener("focus", function () {
    if (last) show(true);
  });

  input.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (box.hidden && last) show(true);
      setActive(active + 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive(active - 1);
    } else if (e.key === "Escape" && !box.hidden) {
      e.preventDefault();   // keep the text; just close the list
      show(false);
    }
  });

  // Enter opens the highlighted result, or the first one.
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var opts = options();
    var target = opts[active >= 0 ? active : 0];
    if (target) target.click();
  });

  box.addEventListener("mousedown", function (e) {
    // Keep the focus in the box when "Show more" is clicked.
    if (e.target.closest(".lr-sr__more")) e.preventDefault();
  });

  box.addEventListener("click", function (e) {
    var more = e.target.closest(".lr-sr__more");
    if (!more || !last) return;
    expanded[more.getAttribute("data-group")] = true;
    render(last);
    input.focus();
  });

  document.addEventListener("click", function (e) {
    if (!form.contains(e.target)) show(false);
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault();
    input.focus();
    input.select();
  });

  // /?q=... opens with the results showing.
  if (input.value.trim()) run();
})();
