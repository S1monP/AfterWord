// Embedded note editor on the notes page right pane.
//
// State machine:
//   * mode 'empty': no editor rendered (the empty card is shown).
//   * mode 'new' (?new=1): blank editor, no note id. Save POSTs
//     /api/v1/notes and navigates to /notes?note=<id>.
//   * mode 'edit' (?note=<id>): editor prefilled. Save PATCHes
//     /api/v1/notes/<id> and stays on the page ("Saved ✓");
//     Delete DELETEs and returns to /notes.
//
// The Save button stays disabled until the title or body differs
// from the last saved state, and a beforeunload guard warns about
// unsaved changes.
(function () {
  var editor = document.getElementById("lr-note-editor");
  if (!editor) return;

  var titleEl = document.getElementById("lr-note-title");
  var bodyEl = document.getElementById("lr-note-body");
  var saveBtn = document.getElementById("lr-note-save");
  var delBtn = document.getElementById("lr-note-delete");
  var savedEl = document.getElementById("lr-note-saved");
  var errEl = document.getElementById("lr-note-error");

  var noteId = editor.getAttribute("data-note-id") || "";
  var initialTitle = editor.getAttribute("data-initial-title") || "";
  var initialBody = editor.getAttribute("data-initial-body") || "";

  function isDirty() {
    return (
      titleEl.value !== initialTitle || bodyEl.value !== initialBody
    );
  }

  function refresh() {
    var dirty = isDirty();
    saveBtn.disabled = !dirty;
    editor.classList.toggle("is-dirty", dirty);
  }

  function hideMsg() {
    savedEl.hidden = true;
    errEl.hidden = true;
  }

  function showErr(msg) {
    errEl.textContent = msg;
    errEl.hidden = false;
    savedEl.hidden = true;
  }

  titleEl.addEventListener("input", function () {
    hideMsg();
    refresh();
  });
  bodyEl.addEventListener("input", function () {
    hideMsg();
    refresh();
  });

  window.addEventListener("beforeunload", function (e) {
    if (isDirty()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  saveBtn.addEventListener("click", function () {
    var title = titleEl.value.trim();
    if (!title) {
      showErr("A title is required.");
      return;
    }
    saveBtn.disabled = true;
    var payload = { title: title, body_md: bodyEl.value };
    var req = noteId
      ? fetch("/api/v1/notes/" + encodeURIComponent(noteId), {
          method: "PATCH",
          credentials: "same-origin",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        })
      : fetch("/api/v1/notes", {
          method: "POST",
          credentials: "same-origin",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload),
        });
    req.then(function (r) {
      return r.json().then(function (j) {
        return { ok: r.ok, j: j };
      });
    }).then(function (res) {
      if (!res.ok) {
        showErr(
          (res.j && res.j.detail) || ("Save failed (" + res.status + ").")
        );
        saveBtn.disabled = false;
        refresh();
        return;
      }
      var id = res.j && res.j.note && res.j.note.id;
      if (!noteId) {
        // Created: load the saved note fresh (updates the URL and
        // the sidebar so the new note shows up selected).
        window.location.href =
          "/notes?note_id=" + encodeURIComponent(id || "");
        return;
      }
      initialTitle = title;
      initialBody = bodyEl.value;
      savedEl.hidden = false;
      refresh();
      var link = document.querySelector(
        '[data-note-link="' + noteId + '"]'
      );
      if (link) link.textContent = title;
    }).catch(function () {
      showErr("Network error — please retry.");
      saveBtn.disabled = false;
      refresh();
    });
  });

  if (delBtn) {
    delBtn.addEventListener("click", function () {
      if (!confirm("Delete this note? This cannot be undone.")) return;
      delBtn.disabled = true;
      fetch(
        "/api/v1/notes/" + encodeURIComponent(noteId),
        { method: "DELETE", credentials: "same-origin" }
      ).then(function (r) {
        if (r.status === 204 || r.ok) {
          window.location.href = "/notes";
        } else {
          delBtn.disabled = false;
          showErr("Delete failed (" + r.status + ").");
        }
      }).catch(function () {
        delBtn.disabled = false;
        showErr("Network error — please retry.");
      });
    });
  }
})();