/* Lectures, Meetings and Other: the list (filter, folding groups), the
   reader (player, transcript, find, rename) and the actions menu. */
(function () {
  "use strict";
  var AW = window.AW;
  var page = document.querySelector(".rpage");
  if (!page) return;
  var pageName = page.dataset.page, noun = page.dataset.noun;
  var reader = document.querySelector("[data-reader]");
  var readerId = reader ? reader.dataset.reader : "";
  var params = new URLSearchParams(location.search);
  var courses = [];
  try { courses = JSON.parse(document.getElementById("courses-data").textContent); } catch (e) {}
  var listUrl = location.pathname + (params.get("mode") ? "?mode=" + params.get("mode") : "");

  var P = {
    pencil: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4",
    export: "M6 3h9l4 4v14H6zM12 10v6M9 13l3 3 3-3",
    download: "M12 4v11M7 10l5 5 5-5M5 20h14",
    move: "M3 6h6l2 2h10v11H3zM11 14h6M15 12l2 2-2 2",
    tag: "M3 12V4h8l10 10-8 8zM7.5 7.5h.01",
    trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
    play: "M8 5v14l11-7z",
    pause: "M7 5h4v14H7zM13 5h4v14h-4z",
  };

  /* --- List: filter and folding groups -------------------------------- */
  var filter = document.querySelector("[data-list-filter]");
  var noMatch = document.querySelector("[data-no-match]");
  if (filter) filter.addEventListener("input", function () {
    var q = filter.value.trim().toLowerCase(), any = false;
    document.querySelectorAll("[data-row]").forEach(function (row) {
      var hit = !q || row.dataset.find.indexOf(q) !== -1;
      row.hidden = !hit; if (hit) any = true;
    });
    document.querySelectorAll("[data-group]").forEach(function (g) {
      var visible = g.querySelectorAll("[data-row]:not([hidden])").length;
      g.hidden = !!q && !visible;
      if (q && visible) g.querySelector("[data-toggle-group]").setAttribute("aria-expanded", "true");
    });
    if (noMatch) noMatch.hidden = !q || any;
  });

  var foldKey = "aw-fold-" + pageName, folded = {};
  try { folded = JSON.parse(localStorage.getItem(foldKey) || "{}"); } catch (e) {}
  var onlyCourse = params.get("course");
  document.querySelectorAll("[data-group]").forEach(function (g) {
    var head = g.querySelector("[data-toggle-group]");
    var hasSelected = !!g.querySelector(".lrow.is-selected:not(.is-auto)");
    var shut = onlyCourse ? g.dataset.group !== onlyCourse : (folded[g.dataset.group] && !hasSelected);
    head.setAttribute("aria-expanded", shut ? "false" : "true");
    head.addEventListener("click", function () {
      var open = head.getAttribute("aria-expanded") !== "true";
      head.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) delete folded[g.dataset.group]; else folded[g.dataset.group] = 1;
      try { localStorage.setItem(foldKey, JSON.stringify(folded)); } catch (e) {}
    });
  });
  if (onlyCourse) {
    var target = document.querySelector('[data-group="' + CSS.escape(onlyCourse) + '"]');
    if (target) target.scrollIntoView({ block: "start" });
  }
  var sel = document.querySelector(".lrow.is-selected");
  if (sel && !AW.isPhone()) sel.scrollIntoView({ block: "nearest" });

  /* --- Player and transcript ---------------------------------------------- */
  var player = document.querySelector("[data-player]");
  var audio = player ? player.querySelector("audio") : null;
  var lines = Array.prototype.slice.call(document.querySelectorAll(".tline[data-t]"));
  function fmt(s) {
    s = Math.max(0, Math.floor(s || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
  }
  var current = null;
  function markLine(t) {
    var found = null;
    for (var i = 0; i < lines.length; i++) { if (+lines[i].dataset.t <= t + 0.25) found = lines[i]; else break; }
    if (found === current) return found;
    if (current) current.classList.remove("is-now");
    if (found) found.classList.add("is-now");
    current = found;
    return found;
  }
  function showTab(name) {
    document.querySelectorAll("[data-tab]").forEach(function (b) { b.setAttribute("aria-pressed", b.dataset.tab === name ? "true" : "false"); });
    if (!AW.isPhone()) { document.querySelectorAll("[data-pane]").forEach(function (p) { p.hidden = false; }); return; }
    document.querySelectorAll("[data-pane]").forEach(function (p) { p.hidden = p.dataset.pane !== name; });
  }
  function seek(ms, playNow) {
    var t = ms / 1000;
    var line = markLine(t);
    if (line) {
      if (AW.isPhone()) showTab("transcript");
      line.scrollIntoView({ block: "center" });
    }
    if (!audio) return;
    audio.currentTime = t;
    if (playNow) audio.play();
  }
  if (audio) {
    var playBtn = player.querySelector("[data-play]");
    var scrub = player.querySelector("[data-scrub]");
    var timeEl = player.querySelector("[data-time]"), durEl = player.querySelector("[data-dur]");
    var speedBtn = player.querySelector("[data-speed]");
    var SPEEDS = [1, 1.25, 1.5, 2, 0.75], speed = 0, dragging = false;
    var setPlayIcon = function () {
      var paused = audio.paused;
      playBtn.innerHTML = '<svg class="ic" aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="' + (paused ? P.play : P.pause) + '"/></svg>';
      playBtn.setAttribute("aria-label", paused ? "Play" : "Pause");
    };
    playBtn.addEventListener("click", function () { if (audio.paused) audio.play(); else audio.pause(); });
    audio.addEventListener("play", setPlayIcon);
    audio.addEventListener("pause", setPlayIcon);
    audio.addEventListener("loadedmetadata", function () { durEl.textContent = fmt(audio.duration); });
    audio.addEventListener("timeupdate", function () {
      timeEl.textContent = fmt(audio.currentTime);
      if (!dragging && audio.duration) scrub.value = Math.round(audio.currentTime / audio.duration * 1000);
      markLine(audio.currentTime);
    });
    scrub.addEventListener("input", function () {
      dragging = true;
      if (audio.duration) timeEl.textContent = fmt(scrub.value / 1000 * audio.duration);
    });
    scrub.addEventListener("change", function () {
      dragging = false;
      if (audio.duration) audio.currentTime = scrub.value / 1000 * audio.duration;
    });
    player.querySelectorAll("[data-skip]").forEach(function (b) {
      b.addEventListener("click", function () { audio.currentTime = Math.max(0, audio.currentTime + (+b.dataset.skip)); });
    });
    speedBtn.addEventListener("click", function () {
      speed = (speed + 1) % SPEEDS.length;
      audio.playbackRate = SPEEDS[speed];
      speedBtn.textContent = SPEEDS[speed] + "×";
    });
  }
  document.addEventListener("click", function (e) {
    var s = e.target.closest("[data-seek]");
    if (s) seek(+s.dataset.seek, true);
  });

  /* Suggested deadlines / to-dos / events: each is added or dismissed by hand. */
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-suggest-do]");
    if (!btn) return;
    var row = btn.closest("[data-suggest]"), action = btn.dataset.suggestDo;
    row.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
    AW.api("POST", "/api/v1/sessions/" + readerId + "/proposals/" + row.dataset.suggest, { action: action }).then(function (r) {
      if (action === "add") {
        AW.toastNext(r.result === "duplicate" ? "Already in your lists" : "Added to your lists");
        location.reload();
        return;
      }
      var box = row.parentNode;
      row.remove();
      if (!box.children.length) {
        var head = box.previousElementSibling; /* the hint line, then the heading */
        if (head) { var h = head.previousElementSibling; head.remove(); if (h) h.remove(); }
        box.remove();
      }
      AW.toast("Dismissed");
    }).catch(function (err) {
      row.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
      AW.toast(err.message || "Could not save that");
    });
  });

  /* Opened from search or "Said in": ?t= (ms) jumps there; ?hl= marks words. */
  document.querySelectorAll("[data-tab]").forEach(function (b) { b.addEventListener("click", function () { showTab(b.dataset.tab); }); });
  var startTab = params.get("t") || params.get("hl") ? "transcript" : "summary";
  showTab(startTab);
  window.addEventListener("resize", function () { showTab(document.querySelector('[data-tab][aria-pressed="true"]') ? document.querySelector('[data-tab][aria-pressed="true"]').dataset.tab : "summary"); });
  if (params.get("t") && lines.length) {
    var ms = +params.get("t");
    if (audio && audio.readyState < 1) audio.addEventListener("loadedmetadata", function () { audio.currentTime = ms / 1000; }, { once: true });
    seek(ms, false);
  } else if (params.get("hl")) {
    var mark = document.querySelector(".tlines mark") || document.querySelector(".rsum mark");
    if (mark) mark.scrollIntoView({ block: "center" });
  }

  /* Find in transcript: only the lines with the words stay. */
  var find = document.querySelector("[data-find]");
  var findNone = document.querySelector("[data-find-none]");
  if (find) find.addEventListener("input", function () {
    var q = find.value.trim().toLowerCase(), any = false;
    document.querySelectorAll(".tline").forEach(function (line) {
      var hit = !q || line.textContent.toLowerCase().indexOf(q) !== -1;
      line.hidden = !hit; if (hit) any = true;
    });
    findNone.hidden = !q || any;
  });

  /* --- Rename ---------------------------------------------------------------- */
  var renameForm = document.querySelector("[data-rename]");
  var titleEl = document.querySelector("[data-reader-title]");
  function startRename() {
    if (!renameForm) return;
    renameForm.hidden = false; titleEl.hidden = true;
    var input = renameForm.querySelector("input");
    input.focus(); input.select();
  }
  function stopRename() { renameForm.hidden = true; titleEl.hidden = false; }
  if (renameForm) {
    renameForm.querySelector("[data-rename-cancel]").addEventListener("click", stopRename);
    renameForm.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.preventDefault(); stopRename(); } });
    renameForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var title = renameForm.querySelector("input").value.trim();
      if (!title) return;
      AW.api("POST", "/api/v1/sessions/" + readerId + "/title", { title: title }).then(function () {
        titleEl.textContent = title;
        document.querySelectorAll('[data-rec-menu][data-id="' + readerId + '"]').forEach(function (b) { b.dataset.title = title; });
        var rowTitle = document.querySelector(".lrow.is-selected .lrow__title");
        if (rowTitle) rowTitle.textContent = title;
        stopRename();
        AW.toast("Renamed");
      }).catch(function (err) { AW.toast(err.message || "Could not rename"); });
    });
    if (params.get("rename")) startRename();
  }

  /* --- The actions menu --------------------------------------------------------- */
  var KINDS = [["Lectures", "lecture"], ["Meetings", "meeting"], ["Notes", "voice_memo"], ["Other", "other"]];
  function recUrl(id) { return location.pathname + "?" + (params.get("mode") ? "mode=" + params.get("mode") + "&" : "") + "session=" + id; }
  function afterLeaving(id, message) {
    AW.toastNext(message);
    location.href = id === readerId ? listUrl : location.href;
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-rec-menu]");
    if (!btn) return;
    e.preventDefault();
    var d = btn.dataset, id = d.id, title = d.title;
    var kind = d.kind || "";
    var kindName = (KINDS.filter(function (k) { return k[1] === kind; })[0] || ["Not sorted"])[0];
    var course = courses.filter(function (c) { return c.id === d.course; })[0];
    var items = [
      { label: "Rename", icon: P.pencil, onSelect: function () { if (id === readerId) startRename(); else location.href = recUrl(id) + "&rename=1"; } },
      { label: "Export as Markdown", icon: P.export, onSelect: function () { location.href = "/sessions/" + id + "/export.md"; } },
    ];
    if (d.audio) items.push({ label: "Download audio", icon: P.download, onSelect: function () {
      var a = document.createElement("a");
      a.href = "/api/v1/sessions/" + id + "/audio"; a.download = title + ".aac";
      document.body.appendChild(a); a.click(); a.remove();
    } });
    items.push({ label: "Move to", icon: P.move, end: kindName, sub: KINDS.map(function (k) {
      return { label: k[0], checked: k[1] === kind, onSelect: function () {
        if (k[1] === kind) return;
        AW.api("POST", "/api/v1/sessions/" + id + "/kind", { kind: k[1] })
          .then(function () { afterLeaving(id, "Moved to " + k[0]); })
          .catch(function (err) { AW.toast(err.message || "Could not move it"); });
      } };
    }) });
    if (kind === "lecture" || kind === "meeting") {
      items.push({ label: "Change course", icon: P.tag, end: course ? course.name : "None", sub: courses.map(function (c) {
        return { label: c.name, swatch: c.cc, checked: course && c.id === course.id, onSelect: function () { setCourse(id, c.id, "Moved to " + c.name); } };
      }).concat([{ label: "No course", swatch: "cc-0", checked: !course, onSelect: function () { setCourse(id, null, "Course removed"); } }]) });
    }
    items.push({ sep: true });
    items.push({ label: "Delete", icon: P.trash, danger: true, onSelect: function () {
      AW.confirm({
        title: "Delete this " + noun + "?", name: title,
        text: "Its audio, transcript and summary are removed for good. To-dos, deadlines and calendar entries it added stay.",
      }).then(function (yes) {
        if (!yes) return;
        AW.api("DELETE", "/api/v1/sessions/" + id)
          .then(function () { afterLeaving(id, "Deleted “" + title + "”"); })
          .catch(function (err) { AW.toast(err.status === 409 ? "It is being processed; try again in a minute" : (err.message || "Could not delete it")); });
      });
    } });
    AW.menu(btn, items, { title: title });
  });
  function setCourse(id, courseId, message) {
    AW.api("POST", "/api/v1/sessions/" + id + "/course", { course_id: courseId })
      .then(function () { AW.toastNext(message); location.reload(); })
      .catch(function (err) { AW.toast(err.message || "Could not change the course"); });
  }

  /* --- Retry, and a quiet refresh while a recording is still being processed --- */
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-retry]");
    if (!b) return;
    b.disabled = true; b.lastChild.textContent = "Working…";
    AW.api("POST", "/api/v1/sessions/" + b.dataset.retry + "/transcribe")
      .then(function () { AW.toastNext("Processed again"); location.reload(); })
      .catch(function (err) { b.disabled = false; b.lastChild.textContent = "Retry"; AW.toast(err.message || "It failed again"); });
  });
  // #actions in the address opens the reader's actions menu.
  if (location.hash === "#actions") {
    var dots = document.querySelector(AW.isPhone() ? ".pbar [data-rec-menu]" : ".reader__dots");
    if (dots) setTimeout(function () { dots.click(); }, 800);
  }

  if (reader && reader.dataset.processing) {
    var poll = setInterval(function () {
      fetch("/api/v1/sessions/" + readerId).then(function (r) { return r.json(); }).then(function (d) {
        var st = d.session && d.session.status;
        if (st === "done" || st === "failed") { clearInterval(poll); location.reload(); }
      }).catch(function () {});
    }, 15000);
  }
})();
