static/js/quarantine-dashboard.js

/**
 * quarantine-dashboard.js
 * Enhancements:
 * - AJAX bulk release/delete with optimistic UI updates and rollback on failure
 * - Per-row AJAX release/delete to avoid full page reloads
 * - Status badge rendering for optimistic updates
 * - Keep existing features (bulk select, optimistic feedback, CSRF handling, exports)
 */

document.addEventListener("DOMContentLoaded", () => {
  const selectAll = document.getElementById("select-all");
  const releaseBtn = document.getElementById("bulk-release");
  const deleteBtn = document.getElementById("bulk-delete");

  function getCheckboxes() {
    return Array.from(document.querySelectorAll("input.bulk-checkbox"));
  }

  // toggle all checkboxes
  selectAll?.addEventListener("change", () => {
    const checked = selectAll.checked;
    getCheckboxes().forEach(cb => (cb.checked = checked));
  });

  function getSelectedIds() {
    return getCheckboxes().filter(cb => cb.checked).map(cb => parseInt(cb.value, 10));
  }

  // helper to read CSRF token meta if present
  function getCsrfToken() {
    try {
      return document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || null;
    } catch (e) {
      return null;
    }
  }

  // small toast helper used across the UI
  function showToast(text, actionText, timeout = 4000, actionCallback) {
    const t = document.createElement("div");
    t.className = "mini-toast";
    t.style.position = "fixed";
    t.style.right = "16px";
    t.style.bottom = "16px";
    t.style.background = "#111";
    t.style.color = "#fff";
    t.style.padding = "10px 14px";
    t.style.borderRadius = "8px";
    t.style.boxShadow = "0 6px 18px rgba(0,0,0,0.6)";
    t.style.zIndex = 9999;
    t.style.display = "inline-flex";
    t.style.alignItems = "center";
    t.textContent = text;

    if (actionText) {
      const a = document.createElement("button");
      a.textContent = actionText;
      a.style.marginLeft = "10px";
      a.style.background = "transparent";
      a.style.border = "1px solid rgba(255,255,255,0.08)";
      a.style.color = "#fff";
      a.style.padding = "4px 8px";
      a.style.borderRadius = "6px";
      a.style.cursor = "pointer";
      a.onclick = () => {
        actionCallback && actionCallback();
        if (document.body.contains(t)) document.body.removeChild(t);
      };
      t.appendChild(a);
    }

    document.body.appendChild(t);
    setTimeout(() => { if (document.body.contains(t)) document.body.removeChild(t); }, timeout);
  }

  // Add or update status badge in row (used for optimistic updates)
  function setRowStatusBadge(qid, statusText, optClass) {
    const row = document.querySelector(`input.bulk-checkbox[value='${qid}']`)?.closest("tr");
    if (!row) return;
    let badge = row.querySelector(".status-badge");
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "status-badge";
      badge.style.marginLeft = "8px";
      badge.style.fontSize = "0.85em";
      badge.style.padding = "4px 8px";
      badge.style.borderRadius = "12px";
      badge.style.color = "#fff";
      badge.style.display = "inline-block";
      const actionsCell = row.querySelector("td:last-child");
      if (actionsCell) actionsCell.prepend(badge);
    }
    badge.textContent = statusText;
    // minimal color variants; callers may pass classes like "released", "deleted", "updating"
    badge.style.background = optClass === "released" ? "#28a745" : (optClass === "deleted" ? "#dc3545" : (optClass === "updating" ? "#6c757d" : "#6c757d"));
  }

  // optimistic DOM update helper for multiple rows
  function optimisticMarkRows(ids, statusText, cssClass) {
    ids.forEach(id => setRowStatusBadge(id, statusText, cssClass));
  }

  // rollback helper: remove status badges we added (simple approach: reload the specific rows by reloading page if needed)
  function rollbackAndRefreshWithNotice(msg) {
    showToast(msg || "Action failed; refreshing to sync state", null, 4000);
    setTimeout(() => location.reload(), 700);
  }

  // unified bulk action sender (AJAX) with optimistic UI
  async function sendBulkAction(path, successMsg, optimisticState = { text: "updating", cls: "updating" }) {
    const ids = getSelectedIds();
    if (!ids.length) return alert("No emails selected");

    // optimistic UI
    optimisticMarkRows(ids, optimisticState.text, optimisticState.cls);

    const headers = {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest"
    };
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRFToken"] = csrf;

    try {
      const res = await fetch(path, {
        method: "POST",
        headers,
        body: JSON.stringify({ ids })
      });

      let payload = {};
      try { payload = await res.json(); } catch (e) { payload = {}; }

      if (!res.ok) {
        throw new Error(payload.error || payload.message || "Action failed");
      }

      // success: set released/deleted
      optimisticMarkRows(ids, successMsg, path.includes("/delete") ? "deleted" : "released");
      showToast(successMsg, null, 2500);
      // keep deterministic: refresh shortly to reflect server state fully
      setTimeout(() => location.reload(), 700);
    } catch (err) {
      console.error("bulk action failed", err);
      rollbackAndRefreshWithNotice("Bulk action failed; reloading");
    }
  }

  releaseBtn?.addEventListener("click", () => sendBulkAction("/bulk/release", "Selected emails released", { text: "releasing…", cls: "updating" }));
  deleteBtn?.addEventListener("click", () => {
    if (!confirm("Delete selected emails permanently?")) return;
    sendBulkAction("/bulk/delete", "Selected emails deleted", { text: "deleting…", cls: "updating" });
  });

  // Per-row AJAX release/delete binding (delegated)
  document.body.addEventListener("click", (ev) => {
    const releaseButton = ev.target.closest("form[action*='/release/'] button[type='submit']");
    const deleteButton = ev.target.closest("form[action*='/delete/'] button[type='submit']");

    if (releaseButton || deleteButton) {
      ev.preventDefault();
      const form = (releaseButton || deleteButton).closest("form");
      const action = form.getAttribute("action");
      const qidMatch = action.match(/\/(release|delete)\/(\d+)/);
      if (!qidMatch) {
        form.submit(); // fallback to normal submit
        return;
      }
      const qid = qidMatch[2];
      const isDelete = qidMatch[1] === "delete";
      const headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest"
      };
      const csrf = getCsrfToken();
      if (csrf) headers["X-CSRFToken"] = csrf;

      // optimistic per-row update
      setRowStatusBadge(qid, isDelete ? "deleting…" : "releasing…", "updating");

      fetch(action, {
        method: "POST",
        headers,
        body: JSON.stringify({}) // server accepts empty POSTs for these endpoints
      }).then(async r => {
        let p = {};
        try { p = await r.json(); } catch (e) { p = {}; }
        if (!r.ok) throw new Error(p.error || p.message || "server failed");
        setRowStatusBadge(qid, isDelete ? "deleted" : "released", isDelete ? "deleted" : "released");
        showToast(isDelete ? "Email deleted" : "Email released", null, 2000);
        setTimeout(() => {
          // remove the row from DOM to reflect outcome
          const row = document.querySelector(`input.bulk-checkbox[value='${qid}']`)?.closest("tr");
          if (row && row.parentElement) row.parentElement.removeChild(row);
        }, 450);
      }).catch(err => {
        console.error("row action failed", err);
        rollbackAndRefreshWithNotice("Row action failed; reloading");
      });

      return;
    }
  });

  // overflow menu toggle (used by per-row actions)
  window.toggleMenu = function (btn) {
    const menu = btn.parentElement.querySelector(".overflow-menu");
    if (!menu) return;
    const shown = menu.classList.toggle("show");
    btn.setAttribute("aria-expanded", shown ? "true" : "false");

    if (shown) {
      const handler = (e) => {
        if (!btn.parentElement.contains(e.target)) {
          menu.classList.remove("show");
          btn.setAttribute("aria-expanded", "false");
          document.removeEventListener("click", handler);
        }
      };
      // defer adding listener to avoid immediately closing when the click opened the menu
      setTimeout(() => document.addEventListener("click", handler), 10);
    }
  };

  // optimistic-feedback: update UI first, post to server, revert on failure
  document.body.addEventListener("click", (ev) => {
    const btn = ev.target.closest(".fb-btn");
    if (!btn) return;

    const qid = btn.dataset.qid;
    const val = btn.dataset.val;
    const group = btn.closest(".feedback-controls");
    if (!qid || !val || !group) return;

    // optimistic: update UI
    group.querySelectorAll(".fb-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");

    // show small undo toast
    showToast("Feedback saved", "Undo", 5000, () => { location.reload(); });

    const headers = {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest"
    };
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRFToken"] = csrf;

    fetch(`/quarantine/feedback/${qid}`, {
      method: "POST",
      headers,
      body: JSON.stringify({ feedback: val })
    })
      .then(async r => {
        let p = {};
        try { p = await r.json(); } catch (e) { p = {}; }
        if (!r.ok) {
          const msg = p.error || p.message || (p.status === "failed" && "server failed") || "save failed";
          throw new Error(msg);
        }
        return p;
      })
      .catch(err => {
        console.error("feedback save failed", err);
        showToast("Failed to save feedback: " + (err.message || "unknown"), "Dismiss", 4000);
        // revert / refresh to keep UI accurate
        setTimeout(() => location.reload(), 800);
      });
  });

  // Export link handler: replace or set 'format' param preserving existing filters
  document.querySelectorAll("[data-export]").forEach(el => {
    el.addEventListener("click", (e) => {
      if (e.ctrlKey || e.metaKey || e.button === 1) return;
      e.preventDefault();
      const fmt = el.dataset.export;
      try {
        const url = new URL(window.location.href);
        url.searchParams.set("format", fmt);
        window.location.href = url.toString();
      } catch (err) {
        window.location.href = el.getAttribute("href");
      }
    });
  });
});
