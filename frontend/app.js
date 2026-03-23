const actionList = document.getElementById("action-list");
const previewTitle = document.getElementById("preview-title");
const previewDescription = document.getElementById("preview-description");
const previewCommand = document.getElementById("preview-command");
const artifactList = document.getElementById("artifact-list");
const particleCanvas = document.getElementById("particle-canvas");
const networkSvg = document.querySelector(".network-svg");
const applyButton = document.getElementById("apply-button");
const statusButton = document.getElementById("status-button");
const runStatus = document.getElementById("run-status");
const runOutput = document.getElementById("run-output");

let currentActionId = null;
let currentRunId = null;
let pollTimer = null;

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json();
}

function renderPreview(preview) {
  const action = preview.action || {};
  previewTitle.textContent = action.label || "Action";
  previewDescription.textContent = action.description || "";
  previewCommand.textContent = (preview.commands || []).map((item) => item.cmd).join("\n\n");
  currentActionId = action.id || currentActionId;
  artifactList.innerHTML = "";
  (preview.artifacts || []).forEach((artifact) => {
    const li = document.createElement("li");
    li.textContent = artifact.path || artifact.label || "";
    artifactList.appendChild(li);
  });
  const status = preview.status || {};
  runStatus.textContent = status.run_status || "idle";
  runOutput.textContent = JSON.stringify(status, null, 2);
}

async function loadPreview(actionId) {
  const preview = await fetchJson(`/api/actions/${actionId}/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values: defaultValuesFor(actionId) }),
  });
  renderPreview(preview);
}

async function loadStatus(actionId) {
  const status = await fetchJson(`/api/actions/${actionId}/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values: defaultValuesFor(actionId) }),
  });
  runStatus.textContent = status.run_status || "idle";
  runOutput.textContent = JSON.stringify(status, null, 2);
}

async function pollRun(runId) {
  const run = await fetchJson(`/api/runs/${runId}`);
  runStatus.textContent = run.status || "unknown";
  runOutput.textContent = JSON.stringify(run, null, 2);
  if (run.status === "queued" || run.status === "running") {
    pollTimer = window.setTimeout(() => pollRun(runId), 1500);
  } else {
    pollTimer = null;
  }
}

async function applyAction(actionId) {
  if (!actionId) return;
  if (pollTimer) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
  runStatus.textContent = "starting";
  runOutput.textContent = "Submitting action...";
  const run = await fetchJson(`/api/actions/${actionId}/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values: defaultValuesFor(actionId), approved: true }),
  });
  currentRunId = run.run_id;
  runStatus.textContent = run.status || "queued";
  runOutput.textContent = JSON.stringify(run, null, 2);
  if (currentRunId) {
    pollRun(currentRunId);
  }
}

function defaultValuesFor(actionId) {
  const presets = {
    "test-aws-connection": {
      region: "us-east-2",
    },
    "deploy-environment": {
      source_env: "full-account-scan",
      target_env: "virgin",
      team: "platform",
      region: "us-east-2",
    },
    "destroy-environment": {
      target_env: "virgin",
      region: "us-east-2",
    },
    "export-backup-to-git": {
      source_env: "full-account-scan",
      organization: "client-org",
      repo_prefix: "backup",
      init_git: true,
      commit: true,
      push: true,
    },
    "create-lambda": {
      function_name: "orders-worker",
      runtime: "python3.12",
      template_id: "sqs-consumer",
      iam_scope: "basic",
      trigger_type: "sqs",
      trigger_source: "arn:aws:sqs:us-east-1:123456789012:orders",
      include_test: true,
    },
    "test-git-connection": {
      organization: "client-org",
      host: "github.com",
      protocol: "https",
      token_env: "CLIENT_GIT_TOKEN",
    },
  };
  return presets[actionId] || {};
}

function createActionButton(action, isActive) {
  const button = document.createElement("button");
  button.className = `action-button${isActive ? " active" : ""}`;
  button.type = "button";
  button.innerHTML = `
    <strong>${action.label}</strong>
    <p>${action.description}</p>
    <div class="action-meta">
      <span>${action.category}</span>
      <span>${action.preview_supported ? "preview" : "apply-only"}</span>
      <span>${action.approval_required ? "approval" : "direct"}</span>
    </div>
  `;
  button.addEventListener("click", async () => {
    document.querySelectorAll(".action-button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    currentRunId = null;
    await loadPreview(action.id);
  });
  return button;
}

async function init() {
  try {
    const actions = await fetchJson("/api/actions");
    const preferredOrder = [
      "test-aws-connection",
      "test-git-connection",
      "deploy-environment",
      "export-backup-to-git",
    ];
    const actionMap = new Map(actions.map((action) => [action.id, action]));
    const curated = preferredOrder.map((id) => actionMap.get(id)).filter(Boolean);
    actionList.innerHTML = "";
    curated.forEach((action, index) => {
      actionList.appendChild(createActionButton(action, index === 0));
    });
    if (curated[0]) {
      await loadPreview(curated[0].id);
    }
  } catch (error) {
    previewTitle.textContent = "Backend unavailable";
    previewDescription.textContent = "Start the local backend server to load actions and previews.";
    previewCommand.textContent = "python frontend/server.py";
    artifactList.innerHTML = "";
  }
}

function revealOnScroll() {
  const targets = document.querySelectorAll("[data-animate], .reveal-card");
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add("is-visible");
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.18 });

  targets.forEach((target, index) => {
    if (target.classList.contains("reveal-card")) {
      target.style.transitionDelay = `${Math.min(index * 70, 280)}ms`;
    }
    observer.observe(target);
  });
}

function initParticleField() {
  if (!particleCanvas) return;
  const ctx = particleCanvas.getContext("2d");
  if (!ctx) return;

  let width = 0;
  let height = 0;
  let dots = [];
  let rafId = 0;
  let time = 0;
  const gridSize = 24;

  function resize() {
    width = particleCanvas.width = window.innerWidth;
    height = particleCanvas.height = document.documentElement.scrollHeight;
    const cols = Math.ceil(width / gridSize);
    const rows = Math.ceil(height / gridSize);
    dots = [];
    for (let row = 0; row < rows; row += 1) {
      for (let col = 0; col < cols; col += 1) {
        dots.push({
          x: col * gridSize + 14,
          y: row * gridSize + 14,
          seed: (row * cols + col) * 0.17,
          strong: Math.random() > 0.92,
        });
      }
    }
  }

  function draw() {
    time += 0.014;
    ctx.clearRect(0, 0, width, height);
    for (const dot of dots) {
      const pulse = (Math.sin(time + dot.seed) + 1) / 2;
      const driftX = Math.cos(time * 0.35 + dot.seed) * 0.9;
      const driftY = Math.sin(time * 0.8 + dot.seed) * 1.4;
      const alpha = dot.strong ? 0.18 + pulse * 0.34 : 0.05 + pulse * 0.14;
      const radius = dot.strong ? 1.1 + pulse * 1.75 : 0.65 + pulse * 1.2;
      ctx.beginPath();
      ctx.fillStyle = dot.strong
        ? `rgba(129, 241, 255, ${alpha})`
        : `rgba(78, 170, 215, ${alpha})`;
      ctx.arc(dot.x + driftX, dot.y + driftY, radius, 0, Math.PI * 2);
      ctx.fill();
    }
    rafId = requestAnimationFrame(draw);
  }

  resize();
  draw();
  window.addEventListener("resize", () => {
    cancelAnimationFrame(rafId);
    resize();
    draw();
  });
}

function initSignalNetwork() {
  if (!networkSvg) return;
  const paths = Array.from(networkSvg.querySelectorAll(".signal-path"));
  const layer = document.getElementById("signal-dots-layer");
  if (!layer || paths.length === 0) return;

  const svgNs = "http://www.w3.org/2000/svg";
  const groups = paths.map((path, index) => {
    const group = document.createElementNS(svgNs, "g");
    group.setAttribute("data-path-group", String(index));
    layer.appendChild(group);

    const length = path.getTotalLength();
    const spacing = 14;
    const count = Math.max(10, Math.floor(length / spacing));
    const dots = [];

    for (let i = 0; i <= count; i += 1) {
      const point = path.getPointAtLength((i / count) * length);
      const dot = document.createElementNS(svgNs, "circle");
      dot.setAttribute("cx", String(point.x));
      dot.setAttribute("cy", String(point.y));
      dot.setAttribute("r", i % 5 === 0 ? "2.4" : "1.8");
      dot.setAttribute("class", "signal-path-dot");
      group.appendChild(dot);
      dots.push(dot);
    }

    return {
      dots,
      speed: Number(path.dataset.speed || 0.7),
      phase: index * 0.18,
    };
  });

  let rafId = 0;
  let time = 0;

  function render() {
    time += 0.018;
    groups.forEach((group) => {
      const total = group.dots.length;
      const waveHead = (time * group.speed + group.phase) % 1;
      const activeWindow = 0.34;
      group.dots.forEach((dot, index) => {
        const ratio = index / Math.max(total - 1, 1);
        let delta = waveHead - ratio;
        if (delta < 0) delta += 1;
        const glow = delta <= activeWindow ? 1 - delta / activeWindow : 0;
        const baseOpacity = 0.08;
        const opacity = baseOpacity + glow * 0.92;
        dot.setAttribute("fill-opacity", opacity.toFixed(3));
        dot.setAttribute(
          "r",
          glow > 0.82 ? "2.8" : glow > 0.5 ? "2.4" : glow > 0.14 ? "2.1" : index % 5 === 0 ? "2.3" : "1.7",
        );
        dot.classList.toggle("active", glow > 0.12);
      });
    });
    rafId = requestAnimationFrame(render);
  }

  render();
  window.addEventListener("beforeunload", () => cancelAnimationFrame(rafId), { once: true });
}

document.addEventListener("DOMContentLoaded", () => {
  revealOnScroll();
  initParticleField();
  initSignalNetwork();
  applyButton?.addEventListener("click", async () => {
    try {
      await applyAction(currentActionId);
    } catch (error) {
      runStatus.textContent = "failed";
      runOutput.textContent = String(error);
    }
  });
  statusButton?.addEventListener("click", async () => {
    try {
      if (currentRunId) {
        await pollRun(currentRunId);
      } else if (currentActionId) {
        await loadStatus(currentActionId);
      }
    } catch (error) {
      runStatus.textContent = "failed";
      runOutput.textContent = String(error);
    }
  });
});

init();
