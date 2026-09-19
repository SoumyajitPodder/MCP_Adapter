const LAYER_COLOR_VAR = {
  agent: "--agent",
  gateway: "--gateway",
  mcp: "--mcp",
  adapter: "--adapter",
  endpoint: "--endpoint",
};

const chatLog = document.getElementById("chat-log");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const traceTrack = document.getElementById("trace-track");
const capabilitiesList = document.getElementById("capabilities-list");

function addMessage(text, who) {
  const wrap = document.createElement("div");
  wrap.className = `msg msg-${who}`;
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;
  wrap.appendChild(bubble);
  chatLog.appendChild(wrap);
  chatLog.scrollTop = chatLog.scrollHeight;
  return wrap;
}

function formatDetail(detail) {
  const entries = Object.entries(detail || {});
  if (entries.length === 0) return "";
  return entries
    .map(([k, v]) => `<span class="k">${k}:</span> ${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join("\n");
}

function renderTrace(steps) {
  traceTrack.innerHTML = "";
  if (!steps || steps.length === 0) {
    traceTrack.innerHTML = '<div class="trace-empty">No trace for that request.</div>';
    return;
  }

  steps.forEach((step) => {
    const el = document.createElement("div");
    el.className = "trace-step" + (step.status === "error" ? " error" : "");
    const colorVar = LAYER_COLOR_VAR[step.layer] || "--text-faint";
    el.style.setProperty("--layer-color", `var(${colorVar})`);

    const row = document.createElement("div");
    row.className = "trace-row";
    row.innerHTML = `
      <span class="trace-layer">${step.layer}</span>
      <span class="trace-action">${step.action.replaceAll("_", " ")}</span>
      <span class="trace-time">+${step.t_ms}ms</span>
    `;
    el.appendChild(row);

    const detailText = formatDetail(step.detail);
    if (detailText) {
      const detail = document.createElement("div");
      detail.className = "trace-detail";
      detail.innerHTML = detailText;
      el.appendChild(detail);
    }

    traceTrack.appendChild(el);
  });
}

function driverUsedFromTrace(steps) {
  const dispatchStep = (steps || []).find((s) => s.layer === "adapter" && s.action === "dispatch");
  return dispatchStep ? dispatchStep.detail.driver : null;
}

async function loadCapabilities(highlightId) {
  const res = await fetch("/api/capabilities");
  const caps = await res.json();
  capabilitiesList.innerHTML = "";
  caps.forEach((cap) => {
    const li = document.createElement("li");
    li.className = `cap-item ${cap.status}`;
    if (highlightId && cap.id === highlightId + "_driver") li.classList.add("just-used");
    li.innerHTML = `
      <span class="cap-dot"></span>
      <span class="cap-layer">${cap.layer}</span>
      <span class="cap-name">${cap.name}</span>
    `;
    capabilitiesList.appendChild(li);
  });
}

async function sendTask(text) {
  addMessage(text, "user");
  chatInput.value = "";

  const thinking = addMessage("Working on it…", "agent");

  try {
    const res = await fetch("/api/task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: text }),
    });
    const data = await res.json();

    thinking.querySelector(".msg-bubble").textContent = data.reply;
    if (data.result && data.result.error) {
      thinking.classList.add("msg-error");
    }

    renderTrace(data.trace);
    loadCapabilities(driverUsedFromTrace(data.trace));
  } catch (err) {
    thinking.querySelector(".msg-bubble").textContent =
      "Couldn't reach the pipeline backend. Is web/app.py running?";
    thinking.classList.add("msg-error");
  }
}

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text) return;
  sendTask(text);
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => sendTask(chip.dataset.prompt));
});

loadCapabilities();
