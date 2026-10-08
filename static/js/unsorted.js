// Bulk-sort interactions for /unsorted.
//
// UX: user picks a kind from the dropdown next to each recording,
// then clicks Save. No auto-submit on dropdown change — that's the
// explicit-confirm requirement. The "Classify all with AI" button
// is a placeholder that shows a toast explaining the LLM path is not
// wired yet (it lands in a later milestone).
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

  // Click "Classify all with AI" -> toast-only placeholder.
  function onBulkClick() {
    showToast(
      "AI classification lands in a later milestone. "
      + "Pick a kind manually for now."
    );
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