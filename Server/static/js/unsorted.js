// Bulk-sort interactions for /unsorted.
//
// UX: user picks a kind from the dropdown next to each recording,
// then clicks Save. No auto-submit on dropdown change — that's the
// explicit-confirm requirement. The "Classify all with AI" button
// calls POST /api/v1/classifications/bulk: the server runs the
// text model on each session's transcript and persists the kind.
// Rows that get classified animate away; anything the model could
// not classify stays for a manual pick.
(function () {
  var list = document.querySelector(".lr-sort__list");
  var toast = document.getElementById("lr-sort-toast");
  var bulkBtn = document.getElementById("lr-sort-bulk");

  function showToast(msg) {
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.add("is-visible");
    // Auto-hide after 4s.
    setTimeout(function () {
      toast.classList.remove("is-visible");
    }, 4000);
  }

  // Per-row: enable the Save button once a kind is selected.
  function refreshSaveButton(row) {
    var sel = row.querySelector(".lr-sort__select");
    var btn = row.querySelector(".lr-sort__save");
    if (!sel || !btn) return;
    btn.disabled = !sel.value;
  }

  // Click "Save" -> send to /api/v1/sessions/<id>/kind, then
  // animate the row away. On error, mark the row red and show a
  // toast.
  function onSaveClick(btn) {
    var row = btn.closest(".lr-sort__row");
    if (!row) return;
    var sel = row.querySelector(".lr-sort__select");
    if (!sel || !sel.value) {
      showToast("Pick a kind before clicking Save.");
      return;
    }
    var sid = row.getAttribute("data-session-id");
    if (!sid) return;
    btn.disabled = true;
    sel.disabled = true;
    fetch(
      "/api/v1/sessions/" + encodeURIComponent(sid) + "/kind",
      {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({kind: sel.value}),
      }
    ).then(function (r) {
      if (!r.ok) {
        sel.disabled = false;
        btn.disabled = false;
        row.classList.add("is-error");
        showToast("Save failed (" + r.status + "). Try again.");
        return;
      }
      row.classList.add("is-leaving");
      // After the CSS transition, remove the row.
      setTimeout(function () {
        row.parentNode.removeChild(row);
        // If the list is now empty, redirect to refresh with the
        // empty-state.
        if (list && list.children.length === 0) {
          window.location.reload();
        }
      }, 220);
    }).catch(function () {
      sel.disabled = false;
      btn.disabled = false;
      row.classList.add("is-error");
      showToast("Network error — please retry.");
    });
  }

  // Click "Classify all with AI" -> bulk-classify every row via the
  // text model. Rows with a successful classification animate away;
  // failures stay in the list for a manual pick.
  function onBulkClick() {
    var rows = list
      ? Array.prototype.slice.call(list.querySelectorAll(".lr-sort__row"))
      : [];
    if (!rows.length) {
      showToast("Nothing to classify.");
      return;
    }
    var ids = rows
      .map(function (row) { return row.getAttribute("data-session-id"); })
      .filter(Boolean);
    if (!ids.length) {
      showToast("Nothing to classify.");
      return;
    }
    bulkBtn.disabled = true;
    showToast("Asking the AI to classify " + ids.length + " recording(s)\u2026");
    fetch("/api/v1/classifications/bulk", {
      method: "POST",
      credentials: "same-origin",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_ids: ids}),
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, j: j}; });
    }).then(function (res) {
      bulkBtn.disabled = false;
      if (!res.ok || !res.j || !res.j.results) {
        showToast("Bulk classification failed (" + (res.ok ? 200 : res.status)
          + "). Try again.");
        return;
      }
      var classified = 0;
      rows.forEach(function (row) {
        var sid = row.getAttribute("data-session-id");
        var result = sid ? res.j.results[sid] : null;
        if (result && result.kind) {
          classified++;
          row.classList.add("is-leaving");
          setTimeout(function () {
            if (row.parentNode) row.parentNode.removeChild(row);
            if (list && list.children.length === 0) {
              window.location.reload();
            }
          }, 220);
        }
      });
      if (classified === ids.length) {
        showToast("Classified all " + classified + " recording(s) with AI.");
      } else if (classified > 0) {
        showToast("Classified " + classified + " of " + ids.length
          + ". The rest need a transcript first \u2014 pick manually or retry later.");
      } else {
        showToast("The AI could not classify these recordings yet \u2014 "
          + "transcripts may be missing. Pick a kind manually for now.");
      }
    }).catch(function () {
      bulkBtn.disabled = false;
      showToast("Network error \u2014 please retry.");
    });
  }

  // Wire up.
  if (list) {
    var rows = list.querySelectorAll(".lr-sort__row");
    rows.forEach(function (row) {
      var sel = row.querySelector(".lr-sort__select");
      var btn = row.querySelector(".lr-sort__save");
      if (sel) {
        sel.addEventListener("change", function () {
          refreshSaveButton(row);
          row.classList.remove("is-error");
        });
      }
      if (btn) {
        btn.addEventListener("click", function () {
          onSaveClick(btn);
        });
      }
    });
  }
  if (bulkBtn) {
    bulkBtn.addEventListener("click", onBulkClick);
  }
})();