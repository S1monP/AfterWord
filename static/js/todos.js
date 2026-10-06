// Inline todo interactions: toggle done, delete. Edit-in-place for
// the title is a Tier-2 nicety; for now the quick-add form is the
// way to add tasks, and the title is shown read-only.
//
// Attaches to any <ul class="lr-todo-list"> (or <ul id="lr-todo-list">)
// on the page. The /todos page has one; the dashboard's Todos card
// has another. Both work the same way.
(function () {
  var lists = document.querySelectorAll(".lr-todo-list");
  if (!lists.length) return;

  function api(path, opts) {
    return fetch(path, Object.assign({
      credentials: "same-origin",
      headers: {"Content-Type": "application/json"}
    }, opts || {}));
  }

  function applyClass(li, status) {
    li.classList.remove("is-done", "is-cancelled");
    if (status === "done") li.classList.add("is-done");
    if (status === "cancelled") li.classList.add("is-cancelled");
  }

  function onList(list) {
    list.addEventListener("change", function (e) {
      var box = e.target.closest(".lr-todo__check");
      if (!box) return;
      var li = box.closest(".lr-todo");
      var id = li.getAttribute("data-todo-id");
      var newStatus = box.checked ? "done" : "open";
      box.disabled = true;
      api("/api/v1/todos/" + encodeURIComponent(id) + "/status", {
        method: "POST",
        body: JSON.stringify({status: newStatus})
      }).then(function (r) {
        if (!r.ok) {
          // Revert the checkbox.
          box.checked = !box.checked;
          return;
        }
        li.setAttribute("data-status", newStatus);
        applyClass(li, newStatus);
      }).catch(function () {
        box.checked = !box.checked;
      }).then(function () {
        box.disabled = false;
      });
    });

    list.addEventListener("click", function (e) {
      var del = e.target.closest(".lr-todo__del");
      if (!del) return;
      var li = del.closest(".lr-todo");
      var id = li.getAttribute("data-todo-id");
      if (!confirm("Delete this task? This cannot be undone.")) return;
      del.disabled = true;
      api("/api/v1/todos/" + encodeURIComponent(id), {method: "DELETE"})
        .then(function (r) {
          if (r.ok || r.status === 204) {
            li.parentNode.removeChild(li);
          } else {
            del.disabled = false;
          }
        })
        .catch(function () { del.disabled = false; });
    });
  }

  lists.forEach(onList);
})();