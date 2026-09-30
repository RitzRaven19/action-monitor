/* Action Monitor Console -- frontend logic.
   Talks to server.py's API; renders the NDJSON stream live as it arrives via
   fetch() + ReadableStream. */

const state = { threadId: null, entityId: "console_agent", turnIndex: 0, busy: false, enforce: false };

const SEV_LABEL = { none: "clean", low: "benign, logged", medium: "scope-creep pattern", high: "suspicious" };
const VERDICTS = [
  ["turn", "this turn"],
  ["session", "this conversation"],
  ["persistent", "this identity (count)"],
  ["weighted", "this identity (sensitivity)"],
];

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function badgeHtml(sev, label) {
  return `<span class="badge sev-${sev}"><span class="dot"></span>${escapeHtml(label || `${sev} — ${SEV_LABEL[sev] || sev}`)}</span>`;
}

function setStatus(text) {
  document.getElementById("sidebar-status").textContent = text || "";
}

async function errorDetail(res) {
  try {
    const body = await res.json();
    return body.detail || `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

async function getJson(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(await errorDetail(res));
  return res.json();
}

// ---------------------------------------------------------------- tabs
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.remove("active");
      b.setAttribute("aria-selected", "false");
    });
    btn.classList.add("active");
    btn.setAttribute("aria-selected", "true");
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    document.getElementById(`view-${btn.dataset.view}`).classList.add("active");
    if (btn.dataset.view === "history") loadHistory();
  });
});

// ---------------------------------------------------------------- conversation lifecycle
async function newConversation() {
  const data = await getJson("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entity_id: state.entityId }),
  });
  state.threadId = data.thread_id;
  state.turnIndex = 0;
  document.getElementById("conv-id").textContent = data.thread_id.slice(0, 8);
  document.getElementById("turn-count").textContent = "0";
  document.getElementById("transcript").innerHTML = "";
}

// The identity is fixed per conversation on the server, so switching it
// starts a fresh conversation under the new name.
document.getElementById("entity-id").addEventListener("change", async (e) => {
  const next = e.target.value.trim() || "console_agent";
  if (next === state.entityId) return;
  state.entityId = next;
  await newConversation();
  await loadFootprint();
  setStatus(`Switched identity to "${next}" — new conversation started.`);
});

document.getElementById("btn-new-conversation").addEventListener("click", async () => {
  await newConversation();
  setStatus("New conversation started.");
});

document.getElementById("btn-clear-identity").addEventListener("click", async () => {
  if (!confirm(`Clear the persistent history for "${state.entityId}"? Past sessions in History are kept.`)) return;
  await getJson(`/api/entities/${encodeURIComponent(state.entityId)}/reset`, { method: "POST" });
  await loadFootprint();
  setStatus(`Cleared persistent history for "${state.entityId}".`);
});

document.getElementById("enforce-toggle").addEventListener("change", (e) => {
  state.enforce = e.target.checked;
  setStatus(state.enforce ? "Enforce mode on: out-of-scope calls will be blocked." : "Monitor mode: calls are flagged, never blocked.");
});

// ---------------------------------------------------------------- identity footprint
function renderFootprint(identity) {
  const el = document.getElementById("footprint");
  const rows = identity.resources.length
    ? identity.resources
        .map((r) => `<li><span class="resource">${escapeHtml(r.resource)}</span><span class="weight">${r.sensitivity.toFixed(1)}</span></li>`)
        .join("")
    : '<li class="empty">nothing yet</li>';
  el.innerHTML =
    `<div class="footprint-totals"><span>DISTINCT: <b>${identity.distinct_count}</b>/3</span>` +
    `<span>WEIGHTED: <b>${identity.weighted_score.toFixed(1)}</b>/4.0</span></div>` +
    `<ul class="footprint-list">${rows}</ul>`;
}

async function loadFootprint() {
  try {
    renderFootprint(await getJson(`/api/entities/${encodeURIComponent(state.entityId)}`));
  } catch (err) {
    document.getElementById("footprint").textContent = `Could not load: ${err.message}`;
  }
}

// ---------------------------------------------------------------- presets
async function loadPresets() {
  const presets = await getJson("/api/presets");
  const select = document.getElementById("preset-select");
  select.innerHTML = presets
    .map((p) => `<option value="${escapeHtml(p.task_id)}" title="${escapeHtml(p.prompt)}">${p.injected ? "⚠️" : "✅"} ${escapeHtml(p.task_id)}</option>`)
    .join("");
}

document.getElementById("btn-send-preset").addEventListener("click", () => {
  const taskId = document.getElementById("preset-select").value;
  if (taskId) sendTurn({ task_id: taskId });
});

document.getElementById("message-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("message-input");
  const text = input.value.trim();
  if (!text || state.busy) return;
  input.value = "";
  sendTurn({ text });
});

function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll("#btn-send-preset, #message-form button, #btn-new-conversation, #btn-clear-identity").forEach((b) => {
    b.disabled = busy;
  });
}

// ---------------------------------------------------------------- sending a turn (streamed)
function renderMessage(role, text) {
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="msg-role">${role}</div><div class="msg-bubble">${escapeHtml(text)}</div>`;
  return el;
}

function renderAssistantShell() {
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML =
    '<div class="msg-role">assistant</div>' +
    '<div class="envelope-strip"></div>' +
    '<div class="msg-bubble">Agent is working...</div>' +
    '<div class="action-feed"></div>' +
    '<div class="verdict-row"></div>' +
    '<div class="baseline-note"></div>';
  return el;
}

function renderEnvelope(env) {
  const tools = env.tools.length ? env.tools.map((t) => `<span class="chip">${escapeHtml(t)}</span>`).join("") : '<span class="chip chip-empty">no tools</span>';
  const resources = env.resources.map((r) => `<span class="chip chip-res">${escapeHtml(r)}</span>`).join("");
  return `<span class="env-label">DECLARED SCOPE</span>${tools}${resources}`;
}

function renderActionRow(event) {
  const row = document.createElement("details");
  row.className = event.blocked ? "action-row action-blocked" : "action-row";
  const failed = !event.blocked && event.outcome && event.outcome !== "ok";
  row.innerHTML =
    "<summary>" +
    `<span class="badge sev-${event.severity}"><span class="dot"></span>${event.severity}</span>` +
    `<span class="tool">${escapeHtml(event.tool_name)}</span>` +
    '<span class="arrow">&rarr;</span>' +
    `<span class="resource">${escapeHtml(event.resource)}</span>` +
    (event.blocked ? '<span class="blocked-tag">blocked</span>' : "") +
    (failed ? '<span class="outcome-err">failed</span>' : "") +
    "</summary>" +
    `<div class="action-reason">${escapeHtml(event.classification)}: ${escapeHtml(event.reason)}` +
    (event.blocked || failed ? `<br>${escapeHtml(event.outcome)}` : "") +
    "</div>";
  return row;
}

async function postMessage(payload) {
  return fetch(`/api/conversations/${state.threadId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, enforce: state.enforce }),
  });
}

async function sendTurn(payload) {
  if (state.busy) return;
  setBusy(true);
  setStatus("");
  const transcript = document.getElementById("transcript");
  try {
    if (!state.threadId) await newConversation();
    let res = await postMessage(payload);
    if (res.status === 404) {
      // Server restarted and forgot this conversation -- start over transparently.
      await newConversation();
      setStatus("The server no longer had that conversation, so a new one was started.");
      res = await postMessage(payload);
    }
    if (!res.ok) {
      transcript.appendChild(renderMessage("assistant", `Request failed: ${await errorDetail(res)}`));
      return;
    }
    await readStream(res, transcript);
  } catch (err) {
    transcript.appendChild(renderMessage("assistant", `Request failed: ${err.message}`));
  } finally {
    setBusy(false);
    transcript.scrollTop = transcript.scrollHeight;
  }
}

async function readStream(res, transcript) {
  let assistantEl = null;
  let actionFeedEl = null;

  const handleEvent = (event) => {
    if (event.type === "user_message") {
      transcript.appendChild(renderMessage("user", event.text));
      assistantEl = renderAssistantShell();
      transcript.appendChild(assistantEl);
      actionFeedEl = assistantEl.querySelector(".action-feed");
    } else if (event.type === "envelope") {
      assistantEl.querySelector(".envelope-strip").innerHTML = renderEnvelope(event);
    } else if (event.type === "action") {
      actionFeedEl.appendChild(renderActionRow(event));
    } else if (event.type === "run_creep") {
      const row = document.createElement("div");
      row.className = "action-row";
      row.innerHTML = `${badgeHtml(event.severity, "scope-creep pass")} ${escapeHtml(event.reason)}`;
      actionFeedEl.appendChild(row);
    } else if (event.type === "error") {
      assistantEl.querySelector(".msg-bubble").textContent = "This turn failed.";
      const row = document.createElement("div");
      row.className = "action-row action-error";
      row.textContent = "ERROR: " + event.message;
      actionFeedEl.appendChild(row);
      state.turnIndex++;
      document.getElementById("turn-count").textContent = state.turnIndex;
    } else if (event.type === "done") {
      assistantEl.querySelector(".msg-bubble").textContent = event.final_text || "(no visible text response)";

      assistantEl.querySelector(".verdict-row").innerHTML = VERDICTS.map(([key, label]) => {
        const reason = event.reasons && event.reasons[key];
        return (
          `<div class="verdict"${reason ? ` title="${escapeHtml(reason)}"` : ""}>` +
          `<span class="verdict-label">${label}</span>${badgeHtml(event.verdicts[key])}</div>`
        );
      }).join("");

      if (event.enforce) {
        const note = document.createElement("div");
        note.className = "enforce-note";
        note.textContent = event.blocked_count
          ? `Enforce mode: ${event.blocked_count} action(s) blocked before they ran.`
          : "Enforce mode: nothing needed blocking.";
        assistantEl.querySelector(".verdict-row").after(note);
      }

      const baselineNote = assistantEl.querySelector(".baseline-note");
      const anyFlagged = Object.values(event.verdicts).some((v) => v !== "none");
      if (event.baseline_hits && event.baseline_hits.length) {
        baselineNote.textContent = `Baseline would have caught: ${event.baseline_hits.join(", ")}`;
      } else if (anyFlagged) {
        baselineNote.textContent = "Baseline would have seen nothing -- the visible text never mentioned it.";
      }

      if (event.identity) renderFootprint(event.identity);
      state.turnIndex++;
      document.getElementById("turn-count").textContent = state.turnIndex;
    }
    transcript.scrollTop = transcript.scrollHeight;
  };

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop();
    for (const line of lines) {
      if (line.trim()) handleEvent(JSON.parse(line));
    }
  }
  if (buffer.trim()) handleEvent(JSON.parse(buffer));
}

// ---------------------------------------------------------------- history
function renderStats(stats) {
  const sev = stats.action_severity;
  const cells = [
    ["sessions", stats.sessions],
    ["identities", stats.identities],
    ["turns", stats.turns],
    ["flagged turns", stats.flagged_turns],
    ["actions", stats.actions],
    ["high-sev actions", sev.high],
    ["blocked", stats.blocked_actions],
  ];
  document.getElementById("stats").innerHTML = cells
    .map(([label, value]) => `<div class="stat"><span class="stat-value">${value}</span><span class="stat-label">${label}</span></div>`)
    .join("");
}

async function loadHistory() {
  const list = document.getElementById("session-list");
  let sessions;
  try {
    const [stats, rows] = await Promise.all([getJson("/api/stats"), getJson("/api/history/sessions")]);
    renderStats(stats);
    sessions = rows;
  } catch (err) {
    list.innerHTML = `<p class="hint">Could not load history: ${escapeHtml(err.message)}</p>`;
    return;
  }
  if (!sessions.length) {
    list.innerHTML = '<p class="hint">No sessions recorded yet -- run something in the Live Console tab first.</p>';
    return;
  }
  list.innerHTML = sessions
    .map(
      (s) => `
    <div class="session-item" data-id="${escapeHtml(s.session_id)}" role="button" tabindex="0" aria-label="Session for ${escapeHtml(s.entity_id)}, ${s.turn_count} turns, worst severity ${s.worst_severity}">
      <div class="s-top"><span class="s-entity">${escapeHtml(s.entity_id)}</span>${badgeHtml(s.worst_severity)}</div>
      <div class="s-meta">${s.turn_count} turn(s) &middot; ${new Date(s.created_at * 1000).toLocaleString()} &middot; ${escapeHtml(s.session_id.slice(0, 8))}</div>
    </div>`
    )
    .join("");
  list.querySelectorAll(".session-item").forEach((el) => {
    el.addEventListener("click", () => selectSession(el.dataset.id, list));
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        selectSession(el.dataset.id, list);
      }
    });
  });
}

async function selectSession(sessionId, list) {
  list.querySelectorAll(".session-item").forEach((el) => el.classList.toggle("active", el.dataset.id === sessionId));
  const container = document.getElementById("session-detail");
  let detail;
  try {
    detail = await getJson(`/api/history/sessions/${encodeURIComponent(sessionId)}`);
  } catch (err) {
    container.innerHTML = `<p class="hint">Could not load session: ${escapeHtml(err.message)}</p>`;
    return;
  }
  const exportUrl = `/api/history/sessions/${encodeURIComponent(sessionId)}/export`;
  container.innerHTML =
    '<div class="detail-head">' +
    `<h3 class="section-label">IDENTITY: <span class="mono">${escapeHtml(detail.entity_id)}</span> &middot; ${detail.runs.length} turn(s)</h3>` +
    `<a class="hud-btn" href="${exportUrl}" download>EXPORT JSON</a>` +
    "</div>" +
    (detail.runs.length ? "" : '<p class="hint">No turns were sent in this session.</p>') +
    detail.runs
      .map(
        (run) => `
      <div class="turn-block">
        <h4>TURN ${run.turn_index}${run.enforce ? ' <span class="blocked-tag">enforced</span>' : ""}</h4>
        <div class="kv"><span class="k">Prompt:</span> ${escapeHtml(run.full_prompt)}</div>
        <div class="kv"><span class="k">Final answer:</span> ${escapeHtml(run.final_text || "(none)")}</div>
        <div class="kv"><span class="k">Actions (${run.actions.length}):</span></div>
        ${run.actions.map((a) => `<div class="flag-row">- ${escapeHtml(a.tool_name)} &rarr; ${escapeHtml(a.resource)} (${escapeHtml(a.outcome)})</div>`).join("")}
        <div class="kv"><span class="k">Flags (${run.flags.length}):</span></div>
        ${run.flags.map((f) => `<div class="flag-row">${badgeHtml(f.severity)} [${escapeHtml(f.scope)}] ${escapeHtml(f.classification)}: ${escapeHtml(f.reason)}</div>`).join("")}
      </div>`
      )
      .join("");
}

// ---------------------------------------------------------------- init
(async () => {
  try {
    await Promise.all([loadPresets(), newConversation(), loadFootprint()]);
  } catch (err) {
    setStatus(`Could not reach the server: ${err.message}`);
  }
})();
