const API = 'https://availabl-backend.onrender.com';

function set(id, val) {
  const el = document.getElementById(id);
  if (el && val !== undefined && val !== null) el.textContent = val;
}

async function loadDashboard() {
  try {
    const res = await fetch(`${API}/api/demo`);
    if (!res.ok) throw new Error(res.status);
    const d = await res.json();

    const inv   = d.inventory   || {};
    const summ  = d.summary     || {};
    const manif = d.deployment_manifest || {};
    const val   = d.validation_report  || {};

    const counts = inv.counts || {};
    const lambda = counts.lambda_functions ?? 0;
    const sqs    = counts.sqs_queues       ?? 0;
    const dynamo = counts.dynamodb_tables  ?? 0;
    const deps   = counts.dependency_nodes ?? 0;

    const created  = summ.created_resources         ?? '--';
    const okChecks = summ.ok_smoke_checks            ?? '--';
    const issues   = summ.validation_issue_checks    ?? '--';

    // Stat cards
    set('stat-resources',       created);
    set('stat-resources-delta', `${lambda} Lambda · ${sqs} SQS · ${dynamo} DDB`);
    set('stat-checks',          okChecks);
    set('stat-checks-delta',    okChecks !== '--' ? `${okChecks} passed` : '');
    set('stat-issues',          issues);
    set('stat-issues-delta',    issues === 0 ? 'No issues' : `${issues} flagged`);

    // Migration row
    const srcEnv = inv.source_env || '';
    const tgtEnv = manif.target_env || '';
    const region = inv.region || '';
    set('mig-id-1',      srcEnv ? `mig-${srcEnv}` : 'mig-sandbox1');
    set('mig-source-1',  srcEnv || 'sandbox1');
    set('mig-target-1',  tgtEnv || 'sandbox2');
    set('mig-date-1',    region || 'us-east-1');

    // Activity
    set('act-smoke',  okChecks);
    set('act-time-1', region ? `${region}` : 'us-east-1');

    // Inventory card
    set('inv-lambda', lambda);
    set('inv-sqs',    sqs);
    set('inv-dynamo', dynamo);
    set('inv-deps',   deps);

  } catch (e) {
    console.warn('Backend unavailable:', e.message);
  }
}

loadDashboard();
