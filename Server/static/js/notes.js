/* Notes: one live editor (formatting shows while typing; stored as
   Markdown), saved as you go; the course picker; the actions menu.
   The list, player and "What was said" timestamps use reading.js. */
(function () {
  "use strict";
  var AW = window.AW;
  var ed = document.querySelector("[data-editor]");
  var courses = [];
  try { courses = JSON.parse(document.getElementById("courses-data").textContent); } catch (e) {}

  /* --- Markdown <-> editor HTML ------------------------------------------ */
  function inline(md) {
    var s = AW.esc(md);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>")
      .replace(/(^|[^\w])_([^_\s][^_]*?)_(?!\w)/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (all, text, url) {
        return /^(https?:|mailto:)/i.test(url) ? '<a href="' + url + '">' + text + "</a>" : all;
      });
    return s;
  }
  function mdToHtml(md) {
    var out = [], list = null;
    function close() { if (list) { out.push("</" + list.tag + ">"); list = null; } }
    function open(tag, cls) {
      if (list && list.tag === tag && list.cls === cls) return;
      close();
      out.push("<" + tag + (cls ? ' class="' + cls + '"' : "") + ">");
      list = { tag: tag, cls: cls };
    }
    String(md || "").replace(/\r/g, "").split("\n").forEach(function (line) {
      var m;
      if (!line.trim()) { close(); return; }
      if ((m = line.match(/^\s*[-*] \[( |x|X)\] ?(.*)$/))) {
        open("ul", "checklist");
        out.push("<li" + (m[1] !== " " ? " data-done" : "") + ">" + (inline(m[2]) || "<br>") + "</li>");
        return;
      }
      if ((m = line.match(/^\s*[-*] (.*)$/))) { open("ul", ""); out.push("<li>" + (inline(m[1]) || "<br>") + "</li>"); return; }
      if ((m = line.match(/^\s*\d+[.)] (.*)$/))) { open("ol", ""); out.push("<li>" + (inline(m[1]) || "<br>") + "</li>"); return; }
      close();
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        var h = m[1].length <= 2 ? "h2" : "h3";
        out.push("<" + h + ">" + inline(m[2]) + "</" + h + ">");
        return;
      }
      out.push("<p>" + inline(line) + "</p>");
    });
    close();
    return out.join("");
  }
  // "** bold **" isn't Markdown: the marks go round the words, the spaces outside.
  function wrap(inner, mark) {
    var m = inner.match(/^(\s*)([\s\S]*?)(\s*)$/);
    return m[2] ? m[1] + mark + m[2] + mark + m[3] : inner;
  }
  function inlineMd(node) {
    var s = "";
    node.childNodes.forEach(function (c) {
      if (c.nodeType === 3) { s += c.nodeValue.replace(/ /g, " "); return; }
      if (c.nodeType !== 1) return;
      var t = c.tagName, inner = inlineMd(c);
      if (t === "BR") s += "\n";
      else if (t === "STRONG" || t === "B") s += wrap(inner, "**");
      else if (t === "EM" || t === "I") s += wrap(inner, "*");
      else if (t === "CODE") s += "`" + inner + "`";
      else if (t === "A") s += "[" + inner + "](" + (c.getAttribute("href") || "") + ")";
      else s += inner;
    });
    return s;
  }
  function htmlToMd(root) {
    var blocks = [];
    root.childNodes.forEach(function (n) {
      if (n.nodeType === 3) { if (n.nodeValue.trim()) blocks.push(n.nodeValue.trim()); return; }
      if (n.nodeType !== 1) return;
      var t = n.tagName;
      if (t === "H1" || t === "H2") { var h2 = inlineMd(n).trim(); if (h2) blocks.push("## " + h2); }
      else if (/^H[3-6]$/.test(t)) { var h3 = inlineMd(n).trim(); if (h3) blocks.push("### " + h3); }
      else if (t === "UL" || t === "OL") {
        var check = n.classList.contains("checklist"), i = 0;
        var items = Array.prototype.map.call(n.children, function (li) {
          i++;
          var text = inlineMd(li).replace(/\n+/g, " ").trim();
          if (check) return "- [" + (li.hasAttribute("data-done") ? "x" : " ") + "] " + text;
          return (t === "OL" ? i + ". " : "- ") + text;
        });
        if (items.length) blocks.push(items.join("\n"));
      } else {
        inlineMd(n).split("\n").forEach(function (line) { if (line.trim()) blocks.push(line.trim()); });
      }
    });
    return blocks.join("\n\n");
  }

  if (!ed) return;
  var body = ed.querySelector("[data-body]");
  // Not [data-title]: the actions button carries that too.
  var titleEl = ed.querySelector("[data-note-title]");
  var noteId = ed.dataset.id || "";
  if (!body) return;
  document.execCommand("defaultParagraphSeparator", false, "p");
  body.innerHTML = mdToHtml(ed.querySelector("[data-md]").value);
  if (!noteId) titleEl.focus();

  /* --- Saving --------------------------------------------------------------- */
  var timer = null, saving = false, again = false;
  function setSaved(text) { document.querySelectorAll("[data-saved]").forEach(function (el) { el.textContent = text; }); }
  function hhmm() { var d = new Date(); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
  function payload() { return { title: titleEl.value.trim() || "Untitled note", body_md: htmlToMd(body) }; }
  function queueSave() {
    clearTimeout(timer);
    setSaved("Editing…");
    timer = setTimeout(save, 700);
  }
  function save() {
    timer = null;
    if (saving) { again = true; return; }
    var data = payload();
    if (!noteId && !titleEl.value.trim() && !data.body_md) { setSaved(""); return; }
    saving = true;
    setSaved("Saving…");
    var req = noteId
      ? AW.api("PATCH", "/api/v1/notes/" + noteId, data)
      : AW.api("POST", "/api/v1/notes", Object.assign({ course_id: ed.dataset.course || null }, data));
    req.then(function (d) {
      if (!noteId) {
        noteId = d.note.id; ed.dataset.id = noteId;
        history.replaceState(null, "", "/notes?note_id=" + noteId);
      }
      setSaved("Saved · " + hhmm());
      var row = document.querySelector('[data-row-id="' + noteId + '"]');
      if (row) {
        row.querySelector("[data-row-title]").textContent = data.title;
        var first = data.body_md.split("\n").filter(function (l) { return l.trim() && l.trim()[0] !== "#"; })[0] || "No text yet";
        row.querySelector("[data-row-first]").textContent = first.replace(/^[-*]\s(\[[ x]\]\s)?/, "");
      }
    }).catch(function (err) { setSaved("Not saved: " + (err.message || "error")); })
      .then(function () { saving = false; if (again) { again = false; save(); } });
  }
  // The title box grows with its text (long titles wrap).
  function fitTitle() { titleEl.style.height = "auto"; titleEl.style.height = titleEl.scrollHeight + "px"; }
  fitTitle();
  window.addEventListener("resize", fitTitle);
  titleEl.addEventListener("input", function () { fitTitle(); queueSave(); });
  titleEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); body.focus(); }
  });
  window.addEventListener("beforeunload", function () {
    if (!timer || !noteId) return;
    clearTimeout(timer);
    fetch("/api/v1/notes/" + noteId, { method: "PATCH", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload()) });
  });

  /* --- Typing: markdown shortcuts turn into formatting ------------------------- */
  function blockOf(node) {
    while (node && node.parentNode !== body) node = node.parentNode;
    return node;
  }
  function currentBlock() {
    var sel = window.getSelection();
    return sel.rangeCount ? blockOf(sel.anchorNode) : null;
  }
  function caretToEnd(el) {
    var r = document.createRange();
    r.selectNodeContents(el); r.collapse(false);
    var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
  }
  function becomes(blk, tag, text) {
    var el = document.createElement(tag);
    el.textContent = text;
    if (!text) el.innerHTML = "<br>";
    blk.replaceWith(el);
    caretToEnd(el);
    return el;
  }
  // ``html`` keeps bold and italic when a line becomes a list item.
  function becomesList(blk, tag, cls, text, html) {
    var prev = blk.previousElementSibling, list;
    if (prev && prev.tagName === tag.toUpperCase() && prev.classList.contains("checklist") === !!cls) list = prev;
    else { list = document.createElement(tag); if (cls) list.className = cls; blk.before(list); }
    var li = document.createElement("li");
    if (html !== undefined) li.innerHTML = html.trim() ? html : "<br>";
    else { li.textContent = text; if (!text) li.innerHTML = "<br>"; }
    list.appendChild(li);
    blk.remove();
    caretToEnd(li);
  }
  // A list item back to a line of its own (splitting the list around it).
  function unlist(li) {
    var list = li.parentNode, p = document.createElement("p");
    p.innerHTML = li.innerHTML || "<br>";
    var after = Array.prototype.slice.call(list.children, Array.prototype.indexOf.call(list.children, li) + 1);
    if (after.length) {
      var rest = list.cloneNode(false);
      after.forEach(function (x) { rest.appendChild(x); });
      list.after(rest);
    }
    list.after(p);
    li.remove();
    if (!list.children.length) list.remove();
    caretToEnd(p);
  }
  body.addEventListener("input", function () {
    var blk = currentBlock();
    if (blk && blk.nodeType === 3) {        // text typed straight into the editor
      var p = document.createElement("p");
      blk.replaceWith(p); p.appendChild(blk); caretToEnd(p);
      blk = p;
    }
    if (blk && blk.nodeType === 1 && (blk.tagName === "P" || blk.tagName === "DIV")) {
      var txt = blk.textContent.replace(/ /g, " "), m;
      if ((m = txt.match(/^(#{2,3}) /))) becomes(blk, "h" + m[1].length, txt.slice(m[0].length));
      else if ((m = txt.match(/^(?:- )?\[ ?\] /))) becomesList(blk, "ul", "checklist", txt.slice(m[0].length));
      else if (/^[-*] /.test(txt)) becomesList(blk, "ul", "", txt.slice(2));
      else if (/^1[.)] /.test(txt)) becomesList(blk, "ol", "", txt.slice(3));
    }
    queueSave();
  });
  body.addEventListener("focus", function () {
    if (!body.firstChild) { body.innerHTML = "<p><br></p>"; caretToEnd(body.firstChild); }
  });
  // Plain text only, so the stored Markdown stays clean.
  body.addEventListener("paste", function (e) {
    e.preventDefault();
    document.execCommand("insertText", false, (e.clipboardData || window.clipboardData).getData("text/plain"));
  });
  // Ticking a checkbox (its box is drawn before the item).
  body.addEventListener("click", function (e) {
    var li = e.target.closest && e.target.closest("ul.checklist > li");
    if (!li || e.clientX - li.getBoundingClientRect().left > 28) return;
    if (li.hasAttribute("data-done")) li.removeAttribute("data-done"); else li.setAttribute("data-done", "");
    queueSave();
  });

  /* --- Formatting buttons -------------------------------------------------------- */
  var tools = ed.querySelector("[data-tools]");
  // Pressing a button mustn't take the focus (and the selection) from the note.
  ["pointerdown", "mousedown"].forEach(function (ev) {
    tools.addEventListener(ev, function (e) { if (e.target.closest("button")) e.preventDefault(); });
  });
  var lastRange = null;
  document.addEventListener("selectionchange", function () {
    var s = window.getSelection();
    if (s.rangeCount && body.contains(s.anchorNode)) lastRange = s.getRangeAt(0).cloneRange();
  });
  tools.addEventListener("click", function (e) {
    var b = e.target.closest("[data-fmt]");
    if (b) { format(b.dataset.fmt); queueSave(); }
    if (e.target.closest("[data-done]")) body.blur();
  });
  function format(kind) {
    if (document.activeElement !== body) {
      body.focus({ preventScroll: true });
      if (lastRange) { var s = window.getSelection(); s.removeAllRanges(); s.addRange(lastRange); }
    }
    if (!body.firstChild) { body.innerHTML = "<p><br></p>"; caretToEnd(body.firstChild); }
    var blk = currentBlock();
    if (kind === "bold" || kind === "italic") { document.execCommand(kind); return; }
    if (kind === "heading") {
      if (blk && /^H[23]$/.test(blk.tagName)) becomes(blk, "p", blk.textContent);
      else if (blk && (blk.tagName === "P" || blk.tagName === "DIV")) becomes(blk, "h2", blk.textContent);
      return;
    }
    if (kind === "list" || kind === "check") {
      var cls = kind === "check" ? "checklist" : "";
      if (blk && (blk.tagName === "UL" || blk.tagName === "OL")) {
        var sel = window.getSelection(), li = sel.anchorNode && (sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentNode).closest("li");
        // The same button again: back to a plain line. The other one: switch the list's kind.
        if (blk.tagName === "UL" && blk.classList.contains("checklist") === !!cls) { if (li) unlist(li); }
        else if (blk.tagName === "OL") { var ul = document.createElement("ul"); if (cls) ul.className = cls; ul.innerHTML = blk.innerHTML; blk.replaceWith(ul); caretToEnd(li ? ul.children[Array.prototype.indexOf.call(blk.children, li)] : ul); }
        else blk.classList.toggle("checklist", !!cls);
      } else if (blk && /^(P|DIV|H2|H3)$/.test(blk.tagName)) {
        becomesList(blk, "ul", cls, "", blk.innerHTML.replace(/^<br>$/, ""));
      }
      return;
    }
    if (kind === "link") {
      var url = window.prompt("Link to (web address):", "https://");
      if (!url || url === "https://") return;
      if (!/^(https?:|mailto:)/i.test(url)) url = "https://" + url;
      if (window.getSelection().isCollapsed) document.execCommand("insertHTML", false, '<a href="' + AW.esc(url) + '">' + AW.esc(url) + "</a>");
      else document.execCommand("createLink", false, url);
    }
  }

  /* --- Phone: the buttons ride above the keyboard while typing ------------------- */
  function placeTools() {
    var vv = window.visualViewport;
    if (!vv) return;
    ed.style.setProperty("--kb", Math.max(0, window.innerHeight - vv.height - vv.offsetTop) + "px");
  }
  body.addEventListener("focus", function () { ed.classList.add("is-editing"); placeTools(); });
  body.addEventListener("blur", function () { setTimeout(function () { if (document.activeElement !== body) ed.classList.remove("is-editing"); }, 150); });
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", placeTools);
    window.visualViewport.addEventListener("scroll", placeTools);
  }

  /* --- Course picker ----------------------------------------------------------- */
  var pick = ed.querySelector("[data-course-pick]");
  if (pick) pick.addEventListener("click", function () {
    var current = ed.dataset.course || "";
    function choose(c) {
      var id = c ? c.id : "";
      function show() {
        ed.dataset.course = id;
        pick.className = "ned__course " + (c ? c.cc : "cc-0");
        pick.querySelector("[data-course-name]").textContent = c ? c.name : "No course";
      }
      if (!noteId) { show(); return; }
      AW.api("PATCH", "/api/v1/notes/" + noteId, { course_id: id || null })
        .then(function () { show(); setSaved("Saved · " + hhmm()); })
        .catch(function (err) { AW.toast(err.message || "Could not change the course"); });
    }
    AW.menu(pick, courses.map(function (c) {
      return { label: c.name, swatch: c.cc, checked: c.id === current, onSelect: function () { choose(c); } };
    }).concat([{ label: "No course", swatch: "cc-0", checked: !current, onSelect: function () { choose(null); } }]), { title: "Course" });
  });

  /* --- The actions menu (rows and the open note) ---------------------------------- */
  var KINDS = [["Lectures", "lecture"], ["Meetings", "meeting"], ["Notes", "voice_memo"], ["Other", "other"]];
  var P = {
    export: "M6 3h9l4 4v14H6zM12 10v6M9 13l3 3 3-3",
    download: "M12 4v11M7 10l5 5 5-5M5 20h14",
    move: "M3 6h6l2 2h10v11H3zM11 14h6M15 12l2 2-2 2",
    trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13",
  };
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-note-menu]");
    if (!btn) return;
    e.preventDefault();
    var d = btn.dataset, id = d.id, title = d.title, rec = d.recording;
    var items = [{ label: "Export as Markdown", icon: P.export, onSelect: function () { location.href = "/notes/" + id + "/export.md"; } }];
    if (d.audio) items.push({ label: "Download audio", icon: P.download, onSelect: function () {
      var a = document.createElement("a"); a.href = "/api/v1/sessions/" + rec + "/audio"; a.download = title + ".aac";
      document.body.appendChild(a); a.click(); a.remove();
    } });
    if (rec) items.push({ label: "Move to", icon: P.move, end: "Notes", sub: KINDS.map(function (k) {
      return { label: k[0], checked: k[1] === "voice_memo", onSelect: function () {
        if (k[1] === "voice_memo") return;
        AW.api("POST", "/api/v1/sessions/" + rec + "/kind", { kind: k[1] })
          .then(function () { AW.toastNext("Moved to " + k[0]); location.href = "/notes"; })
          .catch(function (err) { AW.toast(err.message || "Could not move it"); });
      } };
    }) });
    items.push({ sep: true });
    items.push({ label: "Delete", icon: P.trash, danger: true, onSelect: function () {
      AW.confirm({
        title: "Delete this note?", name: title,
        text: d.audio || rec ? "The note and its audio are removed for good." : "The note is removed for good.",
      }).then(function (yes) {
        if (!yes) return;
        AW.api("DELETE", "/api/v1/notes/" + id)
          .then(function () { AW.toastNext("Deleted “" + title + "”"); location.href = "/notes"; })
          .catch(function (err) { AW.toast(err.message || "Could not delete it"); });
      });
    } });
    AW.menu(btn, items, { title: title });
  });

  AW.notesMarkdown = { toHtml: mdToHtml, toMd: htmlToMd };
})();

/* Opened from search: ?hl= marks the words in the note, without changing
   its text (the CSS Custom Highlight API); typing clears the marks. */
(function () {
  "use strict";
  var hl = new URLSearchParams(location.search).get("hl");
  var body = document.querySelector("[data-body]");
  if (!hl || !body || !window.CSS || !CSS.highlights || typeof Highlight === "undefined") return;
  // Word starts, so "lens" finds "lenses" and "telescopes" finds "telescope".
  var stems = hl.toLowerCase().split(/\s+/).filter(function (w) { return w.length > 1; })
    .map(function (w) { return w.slice(0, Math.max(3, Math.ceil(w.length * 0.75))); });
  var ranges = [];
  var walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  for (var node = walker.nextNode(); node; node = walker.nextNode()) {
    var re = /[\p{L}\p{N}]+/gu, m;
    while ((m = re.exec(node.data))) {
      var word = m[0].toLowerCase();
      if (stems.some(function (s) { return word.indexOf(s) === 0; })) {
        var r = new Range(); r.setStart(node, m.index); r.setEnd(node, m.index + m[0].length);
        ranges.push(r);
      }
    }
  }
  if (!ranges.length) return;
  var found = new Highlight();
  ranges.forEach(function (r) { found.add(r); });
  CSS.highlights.set("aw-found", found);
  var first = ranges[0].startContainer.parentElement;
  if (first) first.scrollIntoView({ block: "center" });
  body.addEventListener("input", function () { CSS.highlights.delete("aw-found"); }, { once: true });
})();
