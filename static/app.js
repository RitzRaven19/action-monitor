/* Action Monitor Console -- frontend logic.
   Talks to server.py's API; renders the NDJSON stream live as it arrives via
   fetch() + ReadableStream, since this isn't Streamlit's rerun model. */

const state = { threadId: null, entityId: "console_agent", turnIndex: 0 };

const SEV_LABEL = { none: "clean", low: "benign, logged", medium: "scope-creep pattern", high: "suspicious" };

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function badgeHtml(sev) {
  return `<span class="badge sev-${sev}"><span class="dot"></span>${sev} &mdash; ${SEV_LABEL[sev] || sev}</span>`;
}

// ---------------------------------------------------------------- tabs
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    document.getElementById(`view-${btn.dataset.view}`).classList.add("active");
    if (btn.dataset.view === "history") loadHistory();
  });
});

// ---------------------------------------------------------------- conversation lifecycle
async function newConversation() {
  const res = await fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entity_id: state.entityId }),
  });
  const data = await res.json();
  state.threadId = data.thread_id;
  state.turnIndex = 0;
  document.getElementById("conv-id").textContent = data.thread_id.slice(0, 8);
  document.getElementById("turn-count").textContent = "0";
  document.getElementById("transcript").innerHTML = "";
}

document.getElementById("entity-id").addEventListener("input", (e) => {
  state.entityId = e.target.value.trim() || "console_agent";
});

document.getElementById("btn-new-conversation").addEventListener("click", newConversation);

document.getElementById("btn-clear-identity").addEventListener("click", async () => {
  await fetch(`/api/entities/${encodeURIComponent(state.entityId)}/reset`, { method: "POST" });
});

// ---------------------------------------------------------------- presets
async function loadPresets() {
  const res = await fetch("/api/presets");
  const presets = await res.json();
  const select = document.getElementById("preset-select");
  select.innerHTML = presets
    .map((p) => `<option value="${p.task_id}">${p.injected ? "⚠️" : "✅"} ${p.task_id}</option>`)
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
  if (!text) return;
  input.value = "";
  sendTurn({ text });
});

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
    '<div class="msg-bubble">Agent is working...</div>' +
    '<div class="action-feed"></div>' +
    '<div class="verdict-row"></div>' +
    '<div class="baseline-note"></div>';
  return el;
}

async function sendTurn(payload) {
  if (!state.threadId) await newConversation();

  const transcript = document.getElementById("transcript");
  let actionFeedEl = null;
  let assistantEl = null;

  const res = await fetch(`/api/conversations/${state.threadId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, entity_id: state.entityId }),
  });

  if (!res.ok) {
    transcript.appendChild(renderMessage("assistant", `Request failed: HTTP ${res.status}`));
    return;
  }

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
      if (!line.trim()) continue;
      handleEvent(JSON.parse(line));
    }
  }

  function handleEvent(event) {
    if (event.type === "user_message") {
      transcript.appendChild(renderMessage("user", event.text));
      assistantEl = renderAssistantShell();
      transcript.appendChild(assistantEl);
      actionFeedEl = assistantEl.querySelector(".action-feed");
      transcript.scrollTop = transcript.scrollHeight;
    } else if (event.type === "action") {
      const row = document.createElement("div");
      row.className = "action-row";
      row.innerHTML =
        `<span class="badge sev-${event.severity}"><span class="dot"></span>${event.severity}</span>` +
        `<span class="tool">${escapeHtml(event.tool_name)}</span>` +
        `<span class="arrow">&rarr;</span>` +
        `<span class="resource">${escapeHtml(event.resource)}</span>`;
      actionFeedEl.appendChild(row);
      transcript.scrollTop = transcript.scrollHeight;
    } else if (event.type === "run_creep") {
      const row = document.createElement("div");
      row.className = "action-row";
      row.innerHTML = `<span class="badge sev-${event.severity}"><span class="dot"></span>scope-creep pass</span> ${escapeHtml(event.reason)}`;
      actionFeedEl.appendChild(row);
    } else if (event.type === "error") {
      const row = document.createElement("div");
      row.className = "action-row";
      row.style.color = "var(--sev-high)";
      row.textContent = "ERROR: " + event.message;
      actionFeedEl.appendChild(row);
    } else if (event.type === "done") {
      const bubble = assistantEl.querySelector(".msg-bubble");
      bubble.textContent = event.final_text || "(no visible text response)";

      const verdictRow = assistantEl.querySelector(".verdict-row");
      verdictRow.innerHTML = ["turn", "session", "persistent"]
        .map((k) => `<div class="verdict"><span class="verdict-label">this ${k}</span>${badgeHtml(event.verdicts[k])}</div>`)
        .join("");

      const baselineNote = assistantEl.querySelector(".baseline-note");
      const anyFlagged = event.verdicts.turn !== "none" || event.verdicts.session !== "none" || event.verdicts.persistent !== "none";
      if (event.baseline_hits && event.baseline_hits.length) {
        baselineNote.textContent = `Baseline would have caught: ${event.baseline_hits.join(", ")}`;
      } else if (anyFlagged) {
        baselineNote.textContent = "Baseline would have seen nothing -- the visible text never mentioned it.";
      }

      state.turnIndex++;
      document.getElementById("turn-count").textContent = state.turnIndex;
      transcript.scrollTop = transcript.scrollHeight;
    }
  }
}

// ---------------------------------------------------------------- history
async function loadHistory() {
  const res = await fetch("/api/history/sessions");
  const sessions = await res.json();
  const list = document.getElementById("session-list");
  if (!sessions.length) {
    list.innerHTML = '<p class="hint">No sessions recorded yet -- run something in the Live Console tab first.</p>';
    return;
  }
  list.innerHTML = sessions
    .map(
      (s) => `
    <div class="session-item" data-id="${s.session_id}">
      <div class="s-top"><span class="s-entity">${escapeHtml(s.entity_id)}</span>${badgeHtml(s.worst_severity)}</div>
      <div class="s-meta">${s.turn_count} turn(s) &middot; ${new Date(s.created_at * 1000).toLocaleString()} &middot; ${s.session_id.slice(0, 8)}</div>
    </div>`
    )
    .join("");
  list.querySelectorAll(".session-item").forEach((el) => {
    el.addEventListener("click", () => selectSession(el.dataset.id, list));
  });
}

async function selectSession(sessionId, list) {
  list.querySelectorAll(".session-item").forEach((el) => el.classList.toggle("active", el.dataset.id === sessionId));
  const res = await fetch(`/api/history/sessions/${sessionId}`);
  const detail = await res.json();
  const container = document.getElementById("session-detail");
  container.innerHTML =
    `<h3 class="section-label">IDENTITY: <span class="mono">${escapeHtml(detail.entity_id)}</span> &middot; ${detail.runs.length} turn(s)</h3>` +
    detail.runs
      .map(
        (run) => `
      <div class="turn-block">
        <h4>TURN ${run.turn_index}</h4>
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
loadPresets();
newConversation();
