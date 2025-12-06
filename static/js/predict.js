// static/js/predict.js
// Regenerated, cleaned and more robust predict form + analysis UI client.
// - Uses async/await consistently
// - Defensive parsing of server responses and fallbacks
// - Clear separation: classify -> enqueue -> poll
// - Safe HTML encoding helpers
// - Keeps lastPrediction on window for analysis chart

document.addEventListener("DOMContentLoaded", () => {
  // Elements
  const form = document.getElementById("predict-form");
  const loading = document.getElementById("loading");
  let resultDiv = document.getElementById("result-container");

  if (!form) {
    console.error("Element with ID 'predict-form' not found.");
    return;
  }
  if (!loading) {
    console.error("Element with ID 'loading' not found.");
    return;
  }
  if (!resultDiv) {
    console.warn("Element with ID 'result-container' not found. Creating one.");
    resultDiv = document.createElement("div");
    resultDiv.id = "result-container";
    form.parentNode.insertBefore(resultDiv, form.nextSibling);
  }

  loading.style.display = "none";

  // Utilities
  function setLoading(show) {
    loading.style.display = show ? "block" : "none";
  }

  function safeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Render a classification response object into the UI
  function renderResultObject(res) {
    const prediction = res.prediction || res.predicted_label || res.pred || "unknown";
    const prob =
      typeof res.probability !== "undefined"
        ? res.probability
        : typeof res.risk_score !== "undefined"
        ? res.risk_score
        : null;
    const pct =
      typeof res.risk_score_percent !== "undefined"
        ? res.risk_score_percent
        : prob !== null
        ? (Number(prob) * 100).toFixed(1)
        : "N/A";
    const riskDisplay = res.risk_score_display ? res.risk_score_display : `${pct}%`;

    // Technical explanation
    let techHtml = "";
    const expl = res.technical_explanation || res.explanations || [];
    if (Array.isArray(expl) && expl.length > 0) {
      techHtml = `<h4>Risk Score Breakdown</h4><ul>`;
      expl.forEach((item) => {
        const feature = safeHtml(item.feature || item.name || "");
        const contrib =
          item.contribution !== undefined && item.contribution !== null
            ? Number(item.contribution).toFixed(4)
            : "N/A";
        const interp = safeHtml(item.interpretation || "");
        const src = safeHtml(item.explainer || "");
        techHtml += `<li><b>${feature}</b> — contribution: ${contrib} — ${interp} <small>(${src})</small></li>`;
      });
      techHtml += `</ul>`;
    }

    // Attacker insights and link analysis
    let attackerHtml = "";
    const ai = res.attacker_insights || {};
    if (ai && (ai.motives || ai.recommendations || ai.detailed_reasoning || ai.link_analysis || ai.top_links)) {
      if (Array.isArray(ai.motives) && ai.motives.length) {
        attackerHtml += `<h4>Attacker Motives</h4><ul>`;
        ai.motives.forEach((m) => {
          attackerHtml += `<li>${safeHtml(m)}</li>`;
        });
        attackerHtml += `</ul>`;
      }
      if (Array.isArray(ai.recommendations) && ai.recommendations.length) {
        attackerHtml += `<h4>Recommendations</h4><ul>`;
        ai.recommendations.forEach((r) => {
          attackerHtml += `<li>${safeHtml(r)}</li>`;
        });
        attackerHtml += `</ul>`;
      }
      if (Array.isArray(ai.top_links) && ai.top_links.length) {
        attackerHtml += `<h4>Top Links</h4><ul>`;
        ai.top_links.forEach((l) => {
          attackerHtml += `<li>${safeHtml(l.host || "")} — ${safeHtml(String(l.suspicion_percent || ""))}%</li>`;
        });
        attackerHtml += `</ul>`;
      }
      if (Array.isArray(ai.link_analysis) && ai.link_analysis.length) {
        attackerHtml += `<h4>Link Analysis</h4><ul>`;
        ai.link_analysis.forEach((l) => {
          const host = safeHtml(l.host || "");
          const up = l.suspicion_percent !== undefined ? `${safeHtml(String(l.suspicion_percent))}%` : `${safeHtml(String(Math.round((l.suspicion || 0) * 100)))}%`;
          const reasons = Array.isArray(l.reasons) ? safeHtml(l.reasons.join("; ")) : "";
          const urlEsc = safeHtml(l.url || "");
          attackerHtml += `<li><a href="${urlEsc}" target="_blank" rel="noopener noreferrer">${host || urlEsc}</a> — ${up}${reasons ? ` — ${reasons}` : ""}</li>`;
        });
        attackerHtml += `</ul>`;
      }
      if (Array.isArray(ai.detailed_reasoning) && ai.detailed_reasoning.length) {
        attackerHtml += `<h4>Detailed Reasoning</h4><ul>`;
        ai.detailed_reasoning.forEach((d) => {
          attackerHtml += `<li>${safeHtml(d)}</li>`;
        });
        attackerHtml += `</ul>`;
      }
    }

    resultDiv.innerHTML = `
      <div class="result">
        <h3>Prediction</h3>
        <p><strong>${safeHtml(prediction)}</strong></p>
        <h4>Risk Score: ${safeHtml(riskDisplay)}</h4>
        ${techHtml}
        ${attackerHtml}
      </div>
    `;

    // expose latest result for analysis chart
    window.lastPrediction = res;

    // Try populate modal lists if present (non-critical)
    try {
      const techList = document.getElementById("technicalExplanationList");
      const attackerList = document.getElementById("attackerInsightsList");
      const recList = document.getElementById("recommendationsList");
      const detailList = document.getElementById("detailedReasoningList");
      const analysisDetails = document.getElementById("analysisDetails");
      const linkList = document.getElementById("linkAnalysisList");

      if (analysisDetails) {
        const summaryHtml = `
          <p><strong>Risk Score:</strong> ${safeHtml(riskDisplay)}</p>
          ${techHtml ? `<div>${techHtml}</div>` : ""}
          ${attackerHtml ? `<div>${attackerHtml}</div>` : ""}
        `;
        analysisDetails.innerHTML = summaryHtml;
      }

      if (techList) {
        techList.innerHTML = "";
        (expl || []).forEach((item) => {
          const li = document.createElement("li");
          const feat = item.feature || item.name || "";
          const contrib = item.contribution !== undefined && item.contribution !== null ? Number(item.contribution).toFixed(4) : "N/A";
          li.textContent = `${feat} — contribution: ${contrib} — ${item.interpretation || ""} (${item.explainer || ""})`;
          techList.appendChild(li);
        });
      }

      if (attackerList) {
        attackerList.innerHTML = "";
        (ai.motives || []).forEach((m) => {
          const li = document.createElement("li");
          li.textContent = m;
          attackerList.appendChild(li);
        });
      }

      if (recList) {
        recList.innerHTML = "";
        (ai.recommendations || []).forEach((r) => {
          const li = document.createElement("li");
          li.textContent = r;
          recList.appendChild(li);
        });
      }

      if (detailList) {
        detailList.innerHTML = "";
        (ai.detailed_reasoning || []).forEach((d) => {
          const li = document.createElement("li");
          li.textContent = d;
          detailList.appendChild(li);
        });
      }

      if (linkList) {
        linkList.innerHTML = "";
        const links = ai.link_analysis || [];
        if (!links.length) {
          const li = document.createElement("li");
          li.textContent = "No links found";
          linkList.appendChild(li);
        } else {
          links.forEach((l) => {
            const li = document.createElement("li");
            const host = l.host || "";
            const pct = l.suspicion_percent !== undefined ? `${l.suspicion_percent}%` : `${Math.round((l.suspicion || 0) * 100)}%`;
            const reasons = Array.isArray(l.reasons) ? l.reasons.join("; ") : "";
            const a = document.createElement("a");
            a.href = l.url || "#";
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.textContent = host || l.url;
            li.appendChild(a);
            li.appendChild(document.createTextNode(` — ${pct}${reasons ? ` — ${reasons}` : ""}`));
            linkList.appendChild(li);
          });
        }
      }
    } catch (e) {
      console.warn("Failed to populate analysis modal details:", e);
    }
  }

  // Polling helper for async tasks
  async function checkStatusUrl(taskId) {
    const urls = [`/api/task_status/${taskId}`, `/task_status/${taskId}`];
    for (const u of urls) {
      try {
        const resp = await fetch(u, { credentials: 'same-origin' });
        if (resp.ok) return resp.json();
      } catch (e) {
        // try next
      }
    }
    throw new Error('No task status endpoint available');
  }

  function pollTaskStatus(taskId, onSuccess, onFailure, interval = 1000, timeout = 45000) {
    const start = Date.now();
    async function poll() {
      try {
        const status = await checkStatusUrl(taskId);
        if (status && status.state === "SUCCESS") {
          onSuccess(status.result || {});
          return;
        } else if (status && status.state === "FAILURE") {
          onFailure(status.error || "Task failed");
          return;
        } else {
          if (Date.now() - start > timeout) {
            onFailure("Task timed out");
          } else {
            setTimeout(poll, interval);
          }
        }
      } catch (err) {
        console.warn("Polling error:", err);
        if (Date.now() - start > timeout) {
          onFailure(err.message || "Polling failed");
        } else {
          setTimeout(poll, interval);
        }
      }
    }
    poll();
  }

  // API calls
  async function callApiClassify(content, extra = {}) {
    const body = Object.assign({ text: content, email: content, content: content }, extra);
    return fetch("/api/classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  async function callApiEnqueue(content, extra = {}) {
    const body = Object.assign({ text: content, email: content, content: content }, extra);
    return fetch("/api/enqueue_classify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  // Form submit handler
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    resultDiv.innerHTML = "";
    setLoading(true);

    const formData = new FormData(form);
    const content = (formData.get("email") || formData.get("text") || formData.get("content") || "").toString().trim();

    if (!content) {
      resultDiv.innerHTML = `<p style="color:#ff6666;">Please enter email content.</p>`;
      setLoading(false);
      return;
    }

    // include optional fields if present
    const extra = {};
    if (formData.get("sender")) extra.sender = formData.get("sender");
    if (formData.get("subject")) extra.subject = formData.get("subject");

    // Try synchronous classify first
    try {
      const response = await callApiClassify(content, extra);

      // non-200 treat as fallback
      if (!response.ok) {
        // try to parse error body
        const errBody = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
        throw errBody;
      }

      const data = await response.json();

      // if enqueued (task_id), poll
      if (data && data.task_id) {
        pollTaskStatus(
          data.task_id,
          (result) => {
            renderResultObject(result);
            setLoading(false);
          },
          (err) => {
            resultDiv.innerHTML = `<p style="color:#ff6666;">${safeHtml(err)}</p>`;
            setLoading(false);
          },
          1200,
          45000
        );
        return;
      }

      // if classification object returned, render
      if (
        data &&
        typeof data === "object" &&
        (data.prediction || data.risk_score || data.probability || data.risk_score_percent)
      ) {
        renderResultObject(data);
        setLoading(false);
        return;
      }

      // otherwise treat as unexpected and fallback
      throw { error: "Unexpected response from /api/classify" };
    } catch (err) {
      // fallback to enqueue
      console.warn("api/classify failed, falling back to api/enqueue_classify:", err);
      try {
        const resp = await callApiEnqueue(content, extra);
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
          throw body;
        }
        const q = await resp.json();
        if (!q || !q.task_id) {
          throw q || { error: "No task_id returned from enqueue" };
        }
        pollTaskStatus(
          q.task_id,
          (result) => {
            renderResultObject(result);
            setLoading(false);
          },
          (errorMsg) => {
            resultDiv.innerHTML = `<p style="color:#ff6666;">${safeHtml(errorMsg)}</p>`;
            setLoading(false);
          },
          1200,
          45000
        );
      } catch (enqErr) {
        console.error("Enqueue failed:", enqErr);
        const message =
          (enqErr && enqErr.error) || (enqErr && enqErr.details) || "Error processing request. Please try again later.";
        resultDiv.innerHTML = `<p style="color:#ff6666;">${safeHtml(message)}</p>`;
        setLoading(false);
      }
    }
  });

  //////////////////////////////////////////////////////
  // Analysis Module: Graph Display of Past Predictions
  //////////////////////////////////////////////////////
  const analysisButton = document.getElementById("analysis-btn");
  const analysisModal = document.getElementById("analysis-modal");

  if (analysisButton && analysisModal) {
    analysisButton.addEventListener("click", () => {
      analysisModal.style.display = "block";

      fetch("/analysis_data")
        .then((response) => {
          if (!response.ok) throw new Error(`analysis_data HTTP ${response.status}`);
          return response.json();
        })
        .then((raw) => {
          if (!raw || !Array.isArray(raw.labels) || !Array.isArray(raw.risk_scores)) {
            console.warn("analysis_data returned invalid shape:", raw);
            const details = document.getElementById("analysisDetails");
            if (details) {
              details.innerHTML = `<p style='color:#666;'>No analysis history available yet.</p>`;
            }
            return;
          }

          // Defensive copy
          let labels = raw.labels.slice();
          let scores = raw.risk_scores.slice();

          // Normalize scores to numeric percent 0-100, keep null as null for gaps
          scores = scores.map((v) => {
            if (v === null || typeof v === "undefined") return null;
            const num = Number(v);
            if (isNaN(num)) return null;
            return num <= 1 ? num * 100 : num;
          });

          // Build points and attempt to sort chronologically if labels are ISO-like
          let points = labels.map((l, i) => ({ label: String(l), score: scores[i] !== undefined ? scores[i] : null, idx: i }));
          const isoLike = points.every((p) => /\d{4}-\d{2}-\d{2}/.test(p.label) || p.label === "Now" || p.label === "?");
          if (isoLike) {
            points.sort((a, b) => {
              if (a.label === "?" && b.label !== "?") return -1;
              if (b.label === "?" && a.label !== "?") return 1;
              if (a.label === "Now" && b.label !== "Now") return 1;
              if (b.label === "Now" && a.label !== "Now") return -1;
              return a.label.localeCompare(b.label);
            });
          }

          labels = points.map((p) => p.label);
          const orderedScores = points.map((p) => (p.score === null ? null : Number(p.score)));

          // Append in-memory latest prediction (window.lastPrediction) if present
          if (window.lastPrediction) {
            try {
              let lastScore = null;
              if (typeof window.lastPrediction.risk_score_percent !== "undefined") {
                lastScore = Number(window.lastPrediction.risk_score_percent);
              } else if (typeof window.lastPrediction.risk_score !== "undefined") {
                lastScore = Number(window.lastPrediction.risk_score) * 100;
              } else if (typeof window.lastPrediction.probability !== "undefined") {
                lastScore = Number(window.lastPrediction.probability) * 100;
              }
              if (!isNaN(lastScore)) {
                const lastLabel = "Now";
                if (labels.length === 0) {
                  labels.push(lastLabel);
                  orderedScores.push(lastScore);
                } else {
                  const lastIdx = labels.length - 1;
                  const lastExisting = orderedScores[lastIdx];
                  if (labels[lastIdx] !== lastLabel) {
                    labels.push(lastLabel);
                    orderedScores.push(lastScore);
                  } else {
                    if (Math.abs((lastExisting || 0) - lastScore) > 0.001) {
                      orderedScores[lastIdx] = lastScore;
                    }
                  }
                }
              }
            } catch (e) {
              console.warn("Could not append latest prediction to analysis data:", e);
            }
          }

          const finalLabels = labels;
          const finalData = orderedScores.map((v) => (v === null ? undefined : Number(v)));

          const canvas = document.getElementById("analysisChart");
          if (!canvas) {
            console.error("Canvas with ID 'analysisChart' not found.");
            const details = document.getElementById("analysisDetails");
            if (details) details.innerHTML = `<p style='color:#666;'>Chart canvas missing.</p>`;
            return;
          }
          const ctx = canvas.getContext("2d");

          if (typeof Chart === "undefined") {
            console.error("Chart.js not loaded. Ensure Chart.js <script> is included before predict.js");
            const details = document.getElementById("analysisDetails");
            if (details) details.innerHTML = `<p style='color:#ff6666;'>Chart library missing.</p>`;
            setTimeout(() => {
              try {
                analysisModal.style.display = "none";
              } catch (e) {}
            }, 1200);
            return;
          }

          if (window.analysisChartInstance) {
            try {
              window.analysisChartInstance.destroy();
            } catch (e) {
              /* ignore */
            }
          }

          window.analysisChartInstance = new Chart(ctx, {
            type: "line",
            data: {
              labels: finalLabels,
              datasets: [
                {
                  label: "Risk Score (%)",
                  data: finalData,
                  borderColor: "rgba(75, 192, 192, 1)",
                  backgroundColor: "rgba(75, 192, 192, 0.15)",
                  fill: true,
                  pointRadius: 3,
                  pointHoverRadius: 6,
                  tension: 0.25,
                  spanGaps: true,
                },
              ],
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              scales: {
                y: {
                  beginAtZero: true,
                  max: 100,
                  title: { display: true, text: "Risk Score (%)" },
                },
                x: {
                  ticks: { maxRotation: 0, autoSkip: true },
                },
              },
              plugins: {
                legend: { display: false },
                tooltip: { mode: "index", intersect: false },
              },
            },
          });

          try {
            if (window.analysisChartInstance && typeof window.analysisChartInstance.resize === "function") {
              window.analysisChartInstance.resize();
            }
            if (window.analysisChartInstance && typeof window.analysisChartInstance.update === "function") {
              window.analysisChartInstance.update();
            }
          } catch (e) {
            console.warn("Chart update/resize failed:", e);
          }
        })
        .catch((err) => {
          console.error("Error fetching analysis data:", err);
          const details = document.getElementById("analysisDetails");
          if (details) {
            details.innerHTML = `<p style='color:#ff6666;'>Unable to load analysis history.</p>`;
          }
        });
    });
  }

  // Close modal interactions
  if (analysisModal) {
    const closeBtn = analysisModal.querySelector(".close-btn");
    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        analysisModal.style.display = "none";
      });
    }
    window.addEventListener("click", (event) => {
      if (event.target === analysisModal) {
        analysisModal.style.display = "none";
      }
    });
  }

  // Optional: analysis details toggle
  document.addEventListener("click", (e) => {
    const toggle = document.getElementById("analysis-details-toggle");
    const panel = document.getElementById("analysisDetailsPanel");
    if (!toggle || !panel) return;
    if (e.target === toggle) {
      const showing = panel.style.display !== "none";
      panel.style.display = showing ? "none" : "block";
      toggle.setAttribute("aria-expanded", String(!showing));
    }
  });
});
