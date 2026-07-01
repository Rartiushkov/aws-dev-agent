function revealOnScroll() {
  const targets = document.querySelectorAll("[data-animate], .reveal-card");
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) {
        return;
      }
      entry.target.classList.add("is-visible");
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.15 });

  targets.forEach((target, index) => {
    if (target.classList.contains("reveal-card")) {
      target.style.transitionDelay = `${Math.min(index * 55, 220)}ms`;
    }
    observer.observe(target);
  });
}

function initParallaxCards() {
  const cards = document.querySelectorAll("[data-parallax]");
  if (!cards.length) {
    return;
  }

  function update() {
    const viewportHeight = window.innerHeight || 1;
    cards.forEach((card) => {
      const strength = Number(card.dataset.parallax || 0);
      const rect = card.getBoundingClientRect();
      const centerOffset = rect.top + rect.height / 2 - viewportHeight / 2;
      const translate = (centerOffset / viewportHeight) * strength * -1;
      card.style.setProperty("--parallax-y", `${translate.toFixed(2)}px`);
    });
  }

  update();
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
}

function initHeroTilt() {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    return;
  }

  const frame = document.querySelector("[data-tilt-frame]");
  if (!frame) {
    return;
  }

  function reset() {
    frame.style.transform = "";
    frame.classList.remove("is-tilting");
  }

  frame.addEventListener("pointermove", (event) => {
    const rect = frame.getBoundingClientRect();
    const px = (event.clientX - rect.left) / rect.width;
    const py = (event.clientY - rect.top) / rect.height;
    const rotateY = (px - 0.5) * 8;
    const rotateX = (0.5 - py) * 8;

    frame.classList.add("is-tilting");
    frame.style.transform = `perspective(1400px) rotateX(${rotateX.toFixed(2)}deg) rotateY(${rotateY.toFixed(2)}deg)`;
  });

  frame.addEventListener("pointerleave", reset);
  frame.addEventListener("pointercancel", reset);
}

function setText(id, value) {
  const node = document.getElementById(id);
  if (node) {
    node.textContent = value;
  }
}

function fillList(id, items) {
  const node = document.getElementById(id);
  if (!node) {
    return;
  }
  node.innerHTML = "";
  items.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item;
    node.appendChild(li);
  });
}

function renderMappings(manifest) {
  const mappingList = document.getElementById("mapping-list");
  if (!mappingList) {
    return;
  }

  const items = [
    {
      title: `${manifest.lambda_functions[0]?.source_function || "Lambda"} -> ${manifest.lambda_functions[0]?.target_function || "target"}`,
      detail: "Function clone with execution role remap and event source mapping.",
    },
    {
      title: `${manifest.sqs_queues[0]?.source_queue || "Queue"} -> ${manifest.sqs_queues[0]?.target_queue || "target"}`,
      detail: "Queue URL and ARN remapped into the target environment.",
    },
    {
      title: `${manifest.dynamodb_tables[0]?.source_table || "Table"} -> ${manifest.dynamodb_tables[0]?.target_table || "target"}`,
      detail: `${manifest.dynamodb_table_items[0]?.copied_item_count || 0} DynamoDB item copied in the sandbox run.`,
    },
    {
      title: `${manifest.vpcs.length} VPC / ${manifest.subnets.length} subnets recreated`,
      detail: "Network resource cloning captured in the manifest for review.",
    },
  ];

  mappingList.innerHTML = "";
  items.forEach((item) => {
    const wrapper = document.createElement("div");
    wrapper.className = "mapping-item";

    const strong = document.createElement("strong");
    strong.textContent = item.title;

    const span = document.createElement("span");
    span.textContent = item.detail;

    wrapper.appendChild(strong);
    wrapper.appendChild(span);
    mappingList.appendChild(wrapper);
  });
}

function renderConsoleFeed(timeline) {
  const feed = document.getElementById("console-feed");
  if (!feed) {
    return;
  }

  feed.innerHTML = "";
  timeline.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "console-row";

    const step = document.createElement("span");
    step.textContent = String(index + 1).padStart(2, "0");

    const text = document.createElement("p");
    text.textContent = item;

    row.appendChild(step);
    row.appendChild(text);
    feed.appendChild(row);
  });
}

async function loadDemoState() {
  const apiBase = document.body.dataset.apiBase || "";

  try {
    const response = await fetch(`${apiBase}/api/demo`, {
      headers: {
        Accept: "application/json",
      },
    });

    if (!response.ok) {
      throw new Error(`Demo request failed with ${response.status}`);
    }

    const demo = await response.json();
    const inventory = demo.inventory;
    const manifest = demo.deployment_manifest;
    const validation = demo.validation_report;
    const ecsClone = demo.ecs_clone;
    const summary = demo.summary;

    setText("hero-demo-account", inventory.account_id);
    setText("hero-demo-region", inventory.region);
    setText("demo-status-title", `Demo org ready: ${inventory.source_env} to ${manifest.target_env}`);
    setText("demo-status-copy", `${summary.created_resources} resources created, ${summary.copied_items} DynamoDB items copied, ${summary.validation_issue_checks} validation checks flagged for review.`);
    setText("metric-source-env", inventory.source_env);
    setText("metric-target-env", manifest.target_env);
    setText("metric-validation", validation.issues_found ? "review needed" : "clean");
    setText("metric-issues", String(summary.validation_issue_checks));

    setText("source-title", `${inventory.source_env} inventory in ${inventory.region}`);
    setText("source-badge", `${inventory.signal_resource_count} signal resources`);
    setText("target-title", `${manifest.target_env} clone manifest`);
    setText("target-badge", `${summary.created_resources} created`);

    const stats = document.getElementById("inventory-stats");
    if (stats) {
      stats.innerHTML = `
        <div class="stat-tile"><span>Functions</span><strong>${inventory.counts.lambda_functions}</strong></div>
        <div class="stat-tile"><span>Queues</span><strong>${inventory.counts.sqs_queues}</strong></div>
        <div class="stat-tile"><span>Tables</span><strong>${inventory.counts.dynamodb_tables}</strong></div>
        <div class="stat-tile"><span>Dependency nodes</span><strong>${inventory.counts.dependency_nodes}</strong></div>
      `;
    }

    renderMappings(manifest);
    renderConsoleFeed(demo.timeline);

    setText(
      "validation-headline",
      validation.issues_found
        ? "Validation completed with review notes"
        : "Validation completed cleanly",
    );
    setText(
      "validation-copy",
      `Smoke checks ran against ${validation.target_env}. ${summary.validation_issue_checks} checks reported issues and ${summary.ok_smoke_checks} finished ok.`,
    );
    setText("smoke-count", String(validation.smoke_checks.length));
    setText("issue-count", String(summary.validation_issue_checks));

    setText(
      "resource-headline",
      `${manifest.roles.length} role, ${manifest.sqs_queues.length} queue, ${manifest.dynamodb_tables.length} table, ${manifest.lambda_functions.length} function`,
    );
    fillList("resource-points", [
      `Source account ${manifest.source_account_id} in ${manifest.source_region}`,
      `Target environment ${manifest.target_env} in ${manifest.region}`,
      `${manifest.dynamodb_table_items[0]?.copied_item_count || 0} DynamoDB item copied in the demo run`,
      `${manifest.subnets.length} subnets and ${manifest.route_tables.length} route table recreated`,
    ]);

    setText(
      "ecs-headline",
      ecsClone.status === "ok"
        ? `${ecsClone.deployed_clusters[0]?.source_cluster} -> ${ecsClone.deployed_clusters[0]?.target_cluster}`
        : "ECS clone not available",
    );
    fillList("ecs-points", [
      `${ecsClone.deployed_clusters.length} cluster clone recorded`,
      `${ecsClone.deployed_task_definitions.length} task definition registered`,
      `${ecsClone.deployed_services.length} ECS service created`,
      ecsClone.deployed_services[0]?.target_service_arn || "No target service ARN found",
    ]);
  } catch (error) {
    setText("demo-status-title", "Demo org unavailable");
    setText("demo-status-copy", error.message);
    setText("metric-validation", "offline");
    setText("metric-issues", "n/a");
    renderConsoleFeed([
      "Could not load backend demo state.",
      "Check the Render backend and /api/demo endpoint.",
    ]);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  revealOnScroll();
  initParallaxCards();
  initHeroTilt();
  loadDemoState();
});
