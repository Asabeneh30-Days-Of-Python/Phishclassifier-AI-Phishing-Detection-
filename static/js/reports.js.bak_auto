(function () {
  const reportsTbody = document.getElementById("reports-tbody");
  const tasksList = document.getElementById("tasks-list");
  const btnRefresh = document.getElementById("btn-refresh");
  const btnGenerate = document.getElementById("btn-generate");
  const genForm = document.getElementById("generate-form");
  const genFeedback = document.getElementById("generate-feedback");

  const taskEmpty = document.getElementById("task-empty");
  const taskContent = document.getElementById("task-content");
  const taskMeta = document.getElementById("task-meta");
  const taskActions = document.getElementById("task-actions");
  const taskLogs = document.getElementById("task-logs");

  const socketBadge = document.getElementById("socket-status-badge");

  // artifact detail placeholders (populated when viewing a task)
  const detailAlert = document.getElementById("detail-alert");
  const artifactLogPre = document.getElementById("artifact-log");
  const artifactFullPre = document.getElementById("artifact-full");
  const btnOpenArtifactsModal = document.getElementById("btn-open-artifacts-modal");

  let selectedTaskId = null;
  let socket = null;
  let tasks = {}; // map task_id -> meta

  function fmtSize(n) {
    if (n === undefined || n === null) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n/1024).toFixed(1) + " KB";
    return (n/(1024*1024)).toFixed(1) + " MB";
  }

  function fmtDate(ts) {
    if (!ts) return "";
    const d = (typeof ts === "number") ? new Date(ts * 1000) : new Date(ts);
    return d.toISOString().replace("T", " ").slice(0, 19);
  }

  function sanitize(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/\&/g,"&amp;").replace(/\</g,"&lt;").replace(/\>/g,"&gt;");
  }

  // --- Fetch helpers include filter params from UI ---
  function buildReportFilters() {
    const params = new URLSearchParams();
    try {
      const start = document.getElementById('start_date').value;
      const end = document.getElementById('end_date').value;
      const domain = document.getElementById('domain').value;
      const min = document.getElementById('min_score').value;
      const max = document.getElementById('max_score').value;
      if (start) params.append('start_date', start);
      if (end) params.append('end_date', end);
      if (domain) params.append('domain', domain);
      if (min) params.append('min_score', min);
      if (max) params.append('max_score', max);
    } catch (e) {
      // ignore missing filter inputs
    }
    return params;
  }

  async function fetchReports() {
    try {
      const res = await fetch("/reports/ui/list", {credentials: "same-origin"});
      if (!res.ok) throw new Error("list failed");
      const j = await res.json();
      const rows = j.reports || [];
      if (!rows.length) {
        reportsTbody.innerHTML = '<tr><td colspan="4" class="text-muted">No reports available</td></tr>';
        return;
      }
      reportsTbody.innerHTML = "";
      for (const r of rows) {
        const tr = document.createElement("tr");
        const rawName = r.name || "";
        // Prefer basename for constructing download path when name might include a path
        const baseName = rawName.indexOf("/") !== -1 ? rawName.split("/").pop() : rawName;
        const downloadUrl = r.download_url || `/reports/download/${encodeURIComponent(baseName)}`;
        tr.innerHTML = `<td>${sanitize(rawName)}</td>
                        <td>${fmtSize(r.size)}</td>
                        <td>${fmtDate(r.mtime)}</td>
                        <td>
                          <a class="btn btn-sm btn-outline-primary mr-1" href="${downloadUrl}" data-download-url="${downloadUrl}">Download</a>
                          <button class="btn btn-sm btn-danger btn-delete" data-name="${baseName}">Delete</button>
                        </td>`;
        reportsTbody.appendChild(tr);
      }
      document.querySelectorAll(".btn-delete").forEach(b => b.addEventListener("click", onDelete));
    } catch (err) {
      reportsTbody.innerHTML = '<tr><td colspan="4" class="text-danger">Failed to load reports</td></tr>';
      console.error("fetchReports:", err);
    }
  }

  async function onDelete(e) {
    const name = e.currentTarget.getAttribute("data-name");
    if (!confirm("Delete report " + name + " ? This action cannot be undone.")) return;
    try {
      const res = await fetch("/reports/ui/delete", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name})
      });
      if (!res.ok) throw new Error("delete failed");
      await fetchReports();
      await fetchTasksList();
      alert("Deleted " + name);
    } catch (err) {
      console.error("delete:", err);
      alert("Failed to delete report");
    }
  }

  // Tasks: render as clickable cards
  function renderTasks() {
    tasksList.innerHTML = "";
    const ids = Object.keys(tasks).sort((a,b) => {
      const la = tasks[a].updated_at || tasks[a].created_at || 0;
      const lb = tasks[b].updated_at || tasks[b].created_at || 0;
      return lb - la;
    });
    if (!ids.length) {
      tasksList.innerHTML = '<div class="list-group-item text-muted small">No active tasks</div>';
      return;
    }
    for (const id of ids) {
      const t = tasks[id];
      const btn = document.createElement("button");
      btn.className = "list-group-item list-group-item-action d-flex justify-content-between align-items-start";
      btn.setAttribute("role","listitem");
      btn.dataset.taskId = id;
      // prefer a stable simple id (persistence_id) for display when available
      const displayId = t.persistence_id || (t.result_meta && t.result_meta.persistence_id) || t.task_id || t.audit_id || id;
      const statusClass = statusToClass(t.status || t.state);
      btn.innerHTML = `<div>
                         <div><strong>${sanitize(t.task_type || t.type || "task")}</strong> <small class="text-muted">#${sanitize(displayId)}</small></div>
                         <div class="small text-muted">${sanitize(t.summary || (t.payload && typeof t.payload === "string" ? t.payload : "" ) || "")}</div>
                       </div>
                       <div class="text-right">
                         <div><span class="badge ${statusClass}">${sanitize(t.status || t.state || "")}</span></div>
                         <div class="small text-muted">${fmtDate(t.updated_at || t.started_at)}</div>
                       </div>`;
      btn.dataset.simpleId = displayId;
      btn.addEventListener("click", () => selectTask(id));
      tasksList.appendChild(btn);
    }
  }

  function statusToClass(s) {
    if (!s) return "badge-secondary";
    s = s.toLowerCase();
    if (s.includes("running") || s === "running") return "badge-primary";
    if (s.includes("pending") || s === "pending" || s === "queued") return "badge-warning";
    if (s.includes("success") || s === "succeeded" || s === "completed") return "badge-success";
    if (s.includes("fail") || s === "failure" || s === "failed" ) return "badge-danger";
    if (s.includes("cancel")) return "badge-secondary";
    return "badge-light";
  }

  // Normalize logs into [{ts, level, message}, ...]
  function normalizeLogs(raw) {
    if (!raw) return [];
    if (Array.isArray(raw)) {
      return raw.map(item => {
        if (!item) return {ts: "", level: "INFO", message: ""};
        if (typeof item === "string") return {ts: "", level: "INFO", message: item};
        return {
          ts: item.ts || item.time || "",
          level: item.level || item.severity || "INFO",
          message: item.message || item.msg || String(item)
        };
      });
    }
    if (typeof raw === "string") {
      try {
        const parsed = JSON.parse(raw);
        return normalizeLogs(parsed);
      } catch (e) {
        return raw.split(/\r?\n/).filter(Boolean).map(l => ({ts: "", level: "INFO", message: l}));
      }
    }
    return [{ts: "", level: "INFO", message: String(raw)}];
  }

  // Select task and show logs + actions + artifacts
  async function selectTask(taskId) {
    selectedTaskId = taskId;
    const t = tasks[taskId];
    if (!t) return;
    taskEmpty.style.display = "none";
    taskContent.style.display = "block";
    taskMeta.innerHTML = `<div><strong>Task</strong>: ${sanitize(taskId)} &nbsp; <small class="text-muted">${sanitize(t.task_type||t.type||"")}</small></div>
                          <div class="small text-muted">User: ${sanitize(t.user || t.owner || "")} &nbsp; • Started: ${fmtDate(t.started_at)}</div>
                          <div class="small text-muted">Payload: <code>${sanitize(JSON.stringify(t.payload || t.summary || {}))}</code></div>`;
    // actions
    taskActions.innerHTML = "";
    if (t.status && (t.status.toLowerCase() === "running" || t.status.toLowerCase() === "pending" || (t.state && t.state.toLowerCase()==="running"))) {
      const btnCancel = document.createElement("button");
      btnCancel.className = "btn btn-sm btn-outline-danger mr-2";
      btnCancel.textContent = "Cancel";
      btnCancel.addEventListener("click", () => cancelTask(taskId));
      taskActions.appendChild(btnCancel);
    }
    if (t.result_meta && t.result_meta.filename) {
      const a = document.createElement("a");
      a.className = "btn btn-sm btn-outline-primary";
      // ensure we use basename only
      const fname = (t.result_meta.filename.indexOf("/") !== -1) ? t.result_meta.filename.split("/").pop() : t.result_meta.filename;
      a.href = `/reports/download/${encodeURIComponent(fname)}`;
      a.textContent = "Download report";
      taskActions.appendChild(a);
    }

    // artifact action: view artifacts modal (populated after fetch)
    if (btnOpenArtifactsModal) {
      btnOpenArtifactsModal.style.display = "inline-block";
      btnOpenArtifactsModal.onclick = () => openArtifactsModalForSelected();
    }

    // If server returned a body_url or indicated body exists, render a download/view body button
    if (t.body_url) {
      const b = document.createElement("a");
      b.className = "btn btn-sm btn-outline-secondary ml-2";
      b.href = t.body_url;
      b.textContent = "View body";
      b.setAttribute("target", "_blank");
      taskActions.appendChild(b);
    } else if (t.body_filename) {
      const b2 = document.createElement("a");
      b2.className = "btn btn-sm btn-outline-secondary ml-2";
      // use basename only
      const bf = (t.body_filename.indexOf("/") !== -1) ? t.body_filename.split("/").pop() : t.body_filename;
      b2.href = `/reports/download/${encodeURIComponent(bf)}`;
      b2.textContent = "View body";
      b2.setAttribute("target", "_blank");
      taskActions.appendChild(b2);
    } else if (t.result_meta && typeof t.result_meta === "object") {
      // fallback: check result_meta for body_filename or body_path
      const bf = t.result_meta.body_filename || t.result_meta.body_path || t.result_meta.filename;
      if (bf) {
        const bf_name = (bf.indexOf("/") !== -1) ? bf.split("/").pop() : bf;
        const b3 = document.createElement("a");
        b3.className = "btn btn-sm btn-outline-secondary ml-2";
        b3.href = `/reports/download/${encodeURIComponent(bf_name)}`;
        b3.textContent = "View body";
        b3.setAttribute("target", "_blank");
        taskActions.appendChild(b3);
      }
    }

    // load last N logs
    taskLogs.textContent = "Loading logs…";
    detailAlert && (detailAlert.innerHTML = "");
    artifactLogPre && (artifactLogPre.textContent = "");
    artifactFullPre && (artifactFullPre.textContent = "");
    try {
      const res = await fetch(`/reports/ui/tasks/${encodeURIComponent(taskId)}`, {credentials: "same-origin"});
      if (!res.ok) throw new Error("task detail failed");
      const j = await res.json();
      // normalize logs from multiple possible shapes
      let rawLogs = j.last_logs || j.logs || [];
      const logs = normalizeLogs(rawLogs);
      if (logs && logs.length) {
        taskLogs.textContent = logs.map(l => `[${l.ts}] ${l.level} ${l.message}`).join("\n");
        taskLogs.scrollTop = taskLogs.scrollHeight;
      } else {
        taskLogs.textContent = "No logs available for this task.";
      }
      // merge server-side meta into local tasks map
      tasks[taskId] = Object.assign({}, tasks[taskId] || {}, j);
      renderTasks();

      // determine simple id (persistence id) for artifact fetches
      const simpleId = j.persistence_id || (j.result_meta && j.result_meta.persistence_id) || (j.body_filename ? j.body_filename.split('.')[0] : null) || j.task_id || j.audit_id;
      if (simpleId) {
        // fetch log and body artifacts (best-effort)
        try {
          const logResp = await fetch(`/reports/log/${encodeURIComponent(simpleId)}`, {credentials: "same-origin"});
          const logText = logResp.ok ? await logResp.text() : null;
          if (artifactLogPre) artifactLogPre.textContent = logText || "(no log available)";
        } catch (e) {
          if (artifactLogPre) artifactLogPre.textContent = "(failed to fetch log)";
        }
        try {
          const bodyResp = await fetch(`/reports/body/${encodeURIComponent(simpleId)}`, {credentials: "same-origin"});
          const bodyText = bodyResp.ok ? await bodyResp.text() : null;
          if (artifactFullPre) artifactFullPre.textContent = bodyText || "(no body available)";
        } catch (e) {
          if (artifactFullPre) artifactFullPre.textContent = "(failed to fetch body)";
        }
        if (detailAlert) {
          const logName = `${simpleId}.log`;
          const bodyName = `${simpleId}.full.body.txt`;
          detailAlert.innerHTML = `Showing ID ${sanitize(String(simpleId))} — log: ${sanitize(logName)}, body: ${sanitize(bodyName)}`;
        }
      } else {
        if (detailAlert) detailAlert.innerHTML = "No persistence ID available for this task";
      }

    } catch (err) {
      taskLogs.textContent = "Failed to load logs";
      console.error("selectTask:", err);
    }
  }

  async function cancelTask(taskId) {
    if (!confirm("Attempt to cancel task " + taskId + " ?")) return;
    try {
      const res = await fetch(`/reports/ui/tasks/${encodeURIComponent(taskId)}/cancel`, {
        method: "POST",
        credentials: "same-origin"
      });
      if (!res.ok) throw new Error("cancel failed");
      // optimistic update
      tasks[taskId] = Object.assign({}, tasks[taskId], {status: "cancelling"});
      renderTasks();
    } catch (err) {
      alert("Failed to cancel task");
      console.error("cancelTask:", err);
    }
  }

  async function fetchTasksList() {
    try {
      const params = buildReportFilters();
      params.append('limit', '50');
      const res = await fetch("/reports/ui/tasks?" + params.toString(), {credentials: "same-origin"});
      if (!res.ok) throw new Error("tasks list failed");
      const j = await res.json();
      // expected: array of audit rows [{task_id, task_type, status, started_at, updated_at, user, summary, payload}]
      tasks = {};
      for (const t of (j.tasks || [])) {
        // prefer task_id, fall back to audit id
        const key = t.task_id || t.audit_id || t.id;
        if (key) {
          // ensure nested result_meta parsed when provided as JSON string
          try {
            if (t.result_meta && typeof t.result_meta === 'string') {
              try { t.result_meta = JSON.parse(t.result_meta); } catch (e) { /* leave as string */ }
            }
          } catch (e) {}
          tasks[key] = t;
        }
      }
      renderTasks();
    } catch (err) {
      tasksList.innerHTML = '<div class="text-danger small">Failed to load tasks</div>';
      console.error("fetchTasksList:", err);
    }
  }

  // optimistic generate flow: create task and either listen for socket events or poll
  async function onGenerate(e) {
    e.preventDefault && e.preventDefault();
    genFeedback.textContent = "";
    const form = new FormData(genForm);
    const payload = {};
    for (const [k, v] of form.entries()) {
      if (v && v !== "") payload[k] = v;
    }
    try {
      btnGenerate.disabled = true;
      btnGenerate.textContent = "Generating…";
      const res = await fetch("/reports/ui/generate", {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const j = await res.json();
      if ((res.status === 202 || res.status === 201) && j.task_id) {
        // optimistic card
        const t = {
          task_id: j.task_id,
          task_type: "report",
          status: "pending",
          payload: payload,
          created_at: j.created_at || Date.now()/1000,
          updated_at: j.created_at || Date.now()/1000,
          user: j.user || ""
        };
        tasks[j.task_id] = t;
        renderTasks();
        selectTask(j.task_id);
        genFeedback.innerHTML = `Queued task <code>${j.task_id}</code>.`;
        // if socket not connected, start polling that single task
        if (!socket) pollTask(j.task_id);
      } else if (res.ok && j.filename) {
        genFeedback.innerHTML = `Report generated: <strong>${j.filename}</strong>`;
        await fetchReports();
        await fetchTasksList();
      } else {
        genFeedback.innerHTML = `<span class="text-danger">Generate failed</span>`;
      }
    } catch (err) {
      console.error("generate:", err);
      genFeedback.innerHTML = `<span class="text-danger">Generate request failed</span>`;
    } finally {
      btnGenerate.disabled = false;
      btnGenerate.textContent = "Generate Report";
    }
  }

  // fallback poller for a single task when socket not available
  async function pollTask(taskId, attempts=0) {
    try {
      const res = await fetch(`/reports/ui/tasks/${encodeURIComponent(taskId)}/logs?limit=1&tail=1`, {credentials: "same-origin"});
      if (res.ok) {
        const j = await res.json();
        const rawLogs = j.logs || j.last_logs || [];
        const logs = normalizeLogs(rawLogs);
        if (selectedTaskId === taskId && logs) {
          taskLogs.textContent = logs.map(l=>`[${l.ts}] ${l.level} ${l.message}`).join("\n");
        }
      }
      // also poll status summary
      const s = await fetch(`/reports/ui/tasks/${encodeURIComponent(taskId)}`, {credentials: "same-origin"});
      if (s.ok) {
        const sj = await s.json();
        tasks[taskId] = Object.assign({}, tasks[taskId]||{}, sj);
        renderTasks();
        if (selectedTaskId === taskId) selectTask(taskId);
        if ((sj.status && (sj.status.toLowerCase()==="running" || sj.status.toLowerCase()==="pending")) && attempts < 30) {
          setTimeout(() => pollTask(taskId, attempts+1), 2000 + attempts*500);
        }
      }
    } catch (err) {
      console.error("pollTask:", err);
    }
  }

  // socket handlers (if server-side socket.io available)
  function setSocketBadge(state) {
    if (!socketBadge) return;
    socketBadge.textContent = `Socket: ${state}`;
    socketBadge.classList.remove("badge-secondary","badge-success","badge-danger","badge-warning");
    if (state === "connected") {
      socketBadge.classList.add("badge-success");
    } else if (state === "connecting") {
      socketBadge.classList.add("badge-warning");
    } else {
      socketBadge.classList.add("badge-danger");
    }
    socketBadge.setAttribute("title", `Socket state: ${state}`);
  }

  function initSocket() {
    try {
      const ioClient = window.io || window.socketIo || window.socketIO;
      if (typeof ioClient === "undefined") {
        setSocketBadge("unavailable");
        console.warn("Socket.IO client global not found (window.io / window.socketIo / window.socketIO). Live updates disabled.");
        return;
      }
      socket = ioClient("/reports", {transports:["websocket","polling"], reconnectionAttempts: 5, timeout: 20000});

      setSocketBadge("connecting");

      socket.on("connect", () => {
        console.debug("reports socket connected");
        setSocketBadge("connected");
        // refresh tasks and reports on connect to reconcile any missed events
        fetchTasksList().catch(()=>{});
        fetchReports().catch(()=>{});
      });

      socket.on("connect_error", (err) => {
        console.warn("reports socket connect_error", err);
        setSocketBadge("connecting");
      });

      socket.on("reconnect_attempt", (n) => {
        console.debug("reports socket reconnect attempt", n);
        setSocketBadge("connecting");
      });

      socket.on("reconnect_failed", () => {
        console.warn("reports socket reconnect failed");
        setSocketBadge("unavailable");
      });

      // Normalize incoming payloads: if task_id missing, map from audit_id; always treat filenames/paths as basenames
      socket.on("task:created", (payload) => {
        if (!payload) return;
        const key = payload.task_id || payload.audit_id || payload.persistence_id;
        if (!key) return;
        // ensure result_meta filename basenames
        if (payload.result_meta && payload.result_meta.filename) {
          payload.result_meta.filename = (payload.result_meta.filename.indexOf("/") !== -1) ? payload.result_meta.filename.split("/").pop() : payload.result_meta.filename;
        }
        // normalize body_filename if present
        if (payload.body_filename) {
          payload.body_filename = (payload.body_filename.indexOf("/") !== -1) ? payload.body_filename.split("/").pop() : payload.body_filename;
        }
        // prefer persistence_id for indexing/display
        if (payload.persistence_id) {
          payload.persistence_id = payload.persistence_id;
        } else if (payload.result_meta && payload.result_meta.persistence_id) {
          payload.persistence_id = payload.result_meta.persistence_id;
        }
        tasks[key] = Object.assign({}, tasks[key]||{}, payload);
        renderTasks();
      });
      socket.on("task:updated", (payload) => {
        if (!payload) return;
        const key = payload.task_id || payload.audit_id || payload.persistence_id;
        if (!key) return;
        if (payload.result_meta && payload.result_meta.filename) {
          payload.result_meta.filename = (payload.result_meta.filename.indexOf("/") !== -1) ? payload.result_meta.filename.split("/").pop() : payload.result_meta.filename;
        }
        if (payload.body_filename) {
          payload.body_filename = (payload.body_filename.indexOf("/") !== -1) ? payload.body_filename.split("/").pop() : payload.body_filename;
        }
        // When backend emits only audit_id and no task_id, store under audit id key so UI can reference it
        // Merge persistence_id if provided
        if (!payload.persistence_id && payload.result_meta && payload.result_meta.persistence_id) {
          payload.persistence_id = payload.result_meta.persistence_id;
        }
        tasks[key] = Object.assign({}, tasks[key]||{}, payload);
        renderTasks();
        if (selectedTaskId === key) selectTask(key);
      });
      socket.on("task:log", (payload) => {
        // payload may carry audit_id instead of task_id
        if (!payload) return;
        const rawId = payload.task_id || payload.audit_id || payload.persistence_id;
        if (!rawId) return;
        // If frontend is tracking by task_id but backend provided audit_id only, accept that as key
        const tid = rawId;
        if (selectedTaskId === tid) {
          const line = `[${payload.ts}] ${payload.level} ${payload.message}`;
          taskLogs.textContent = (taskLogs.textContent ? taskLogs.textContent + "\n" : "") + line;
          taskLogs.scrollTop = taskLogs.scrollHeight;
        }
        // Merge minimal log into stored tasks if present
        if (tasks[tid]) {
          // append to last_logs if present
          if (!tasks[tid].last_logs) tasks[tid].last_logs = [];
          tasks[tid].last_logs.push({ts: payload.ts, level: payload.level, message: payload.message});
          renderTasks();
        }
      });
      socket.on("disconnect", () => {
        console.debug("reports socket disconnected");
        setSocketBadge("disconnected");
      });

      setTimeout(() => {
        if (!socket || !socket.connected) {
          setSocketBadge("unavailable");
        }
      }, 7000);

    } catch (err) {
      console.warn("socket init failed", err);
      socket = null;
      setSocketBadge("unavailable");
    }
  }

  // --- Artifacts modal utilities ---
  function openArtifactsModalForSelected() {
    if (!selectedTaskId) return;
    const t = tasks[selectedTaskId];
    if (!t) return;
    const simpleId = t.persistence_id || (t.result_meta && t.result_meta.persistence_id) || t.task_id || t.audit_id;
    // Build rows for modal using current artifact fields
    const row = {
      simple_id: String(simpleId || ""),
      log_text: artifactLogPre ? artifactLogPre.textContent : "(no log)",
      full_text: artifactFullPre ? artifactFullPre.textContent : "(no body)"
    };
    openArtifactsModal([row]);
  }

  // Creates and opens a simple modal listing artifacts (log / full body)
  function openArtifactsModal(rows) {
    // create modal container if not present
    let modal = document.getElementById('artifacts-modal');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'artifacts-modal';
      modal.className = 'modal fade';
      modal.tabIndex = -1;
      modal.setAttribute('role', 'dialog');
      modal.innerHTML = `<div class="modal-dialog modal-xl" role="document">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">Artifacts</h5>
            <button type="button" class="close" data-dismiss="modal" aria-label="Close">
              <span aria-hidden="true">&times;</span>
            </button>
          </div>
          <div class="modal-body">
            <div id="artifacts-table-container"></div>
          </div>
        </div>
      </div>`;
      document.body.appendChild(modal);
    }
    const container = modal.querySelector('#artifacts-table-container');
    if (container) {
      // build table of rows
      let html = '<div class="table-responsive"><table class="table table-sm"><thead><tr><th>ID</th><th style="width:45%">Log</th><th style="width:45%">Full Body</th></tr></thead><tbody>';
      for (const r of rows) {
        html += `<tr>
          <td><code>${sanitize(r.simple_id)}</code></td>
          <td><pre style="max-height:300px;overflow:auto;white-space:pre-wrap;">${sanitize(r.log_text)}</pre></td>
          <td><pre style="max-height:300px;overflow:auto;white-space:pre-wrap;">${sanitize(r.full_text)}</pre></td>
        </tr>`;
      }
      html += '</tbody></table></div>';
      container.innerHTML = html;
    }
    // show modal using Bootstrap's modal (if available) or fallback to alert
    if (window.jQuery && typeof window.jQuery(modal).modal === 'function') {
      window.jQuery(modal).modal('show');
    } else {
      // fallback: open new window with content
      const w = window.open('', '_blank', 'noopener');
      w.document.write('<html><head><title>Artifacts</title></head><body>' + (container ? container.innerHTML : '') + '</body></html>');
      w.document.close();
    }
  }

  // fetch initial state
  btnRefresh.addEventListener("click", function (e) {
    // disable while fetching
    btnRefresh.disabled = true;
    Promise.all([fetchReports(), fetchTasksList()]).finally(()=>{ btnRefresh.disabled = false; });
  });
  btnGenerate.addEventListener("click", onGenerate);

  // initial load
  (async function init() {
    await fetchReports();
    await fetchTasksList();
    initSocket();
  })();

})();
