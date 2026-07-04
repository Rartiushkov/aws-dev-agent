import { getIdToken, requireAuth } from './auth.js';

const API = 'https://availabl-backend.onrender.com';

function set(id, val) {
  const el = document.getElementById(id);
  if (el && val !== undefined && val !== null) el.textContent = val;
}

function renderRows(jobs) {
  const container = document.querySelector('.section-card-body');
  if (!container) {
    return;
  }

  const header = `
    <div class="migration-row-header">
      <span class="row-header-label">Name</span>
      <span class="row-header-label">Source</span>
      <span class="row-header-label">Target</span>
      <span class="row-header-label">Status</span>
      <span class="row-header-label">Date</span>
    </div>
  `;

  if (!jobs.length) {
    container.innerHTML = `${header}<div class="migration-row"><div class="migration-meta">No scans yet. Start with "New scan".</div></div>`;
    return;
  }

  const rows = jobs.slice(0, 3).map((job) => {
    const statusClass = job.status === 'completed'
      ? 'badge-success'
      : job.status === 'failed'
        ? 'badge-pending'
        : 'badge-pending';
    const title = `${job.src_account || 'source'} scan`;
    const date = job.created_at ? new Date(job.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '—';
    return `
      <div class="migration-row" onclick="window.location='migrations.html'">
        <div class="migration-name">
          <div class="migration-icon"><svg viewBox="0 0 16 16"><path d="M2 8h12M10 4l4 4-4 4"/></svg></div>
          <div>
            <div class="migration-title">${title}</div>
            <div class="migration-id">${job.id}</div>
          </div>
        </div>
        <div class="migration-meta">${job.src_account || '—'}</div>
        <div class="migration-meta">${job.src_region || '—'}</div>
        <div><span class="badge ${statusClass}">${job.status}</span></div>
        <div class="migration-meta">${date}</div>
      </div>
    `;
  }).join('');

  container.innerHTML = header + rows;
}

function renderInventory(summary) {
  const counts = summary?.counts || {};
  set('inv-lambda', counts.lambda_functions ?? '--');
  set('inv-sqs', counts.sqs_queues ?? '--');
  set('inv-dynamo', counts.dynamodb_tables ?? '--');
  set('inv-deps', counts.dependency_nodes ?? '--');
}

export async function loadDashboard() {
  let demoData = null;
  try {
    const demoRes = await fetch(`${API}/api/demo`);
    if (demoRes.ok) {
      demoData = await demoRes.json();
    }
  } catch (_) {}

  try {
    await requireAuth();
    const token = await getIdToken();
    const res = await fetch(`${API}/api/scans`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) {
      throw new Error(`Scan list failed with ${res.status}`);
    }

    const payload = await res.json();
    const jobs = payload.items || [];
    const completed = jobs.filter((job) => job.status === 'completed');
    const failed = jobs.filter((job) => job.status === 'failed');
    const latest = completed[0] || jobs[0] || null;
    const latestSummary = latest?.result?.summary || null;

    set('stat-migrations', jobs.length);
    set('stat-resources', latestSummary?.signal_resource_count ?? demoData?.summary?.created_resources ?? '--');
    set('stat-resources-delta', latestSummary ? `${latestSummary.lambda_functions || 0} Lambda · ${latestSummary.sqs_queues || 0} SQS · ${latestSummary.dynamodb_tables || 0} DDB` : 'No completed scans yet');
    set('stat-checks', demoData?.summary?.ok_smoke_checks ?? '--');
    set('stat-checks-delta', latest ? latest.message : 'No scans yet');
    set('stat-issues', failed.length);
    set('stat-issues-delta', failed.length ? `${failed.length} failed scans need review` : 'No failed scans');
    set('act-smoke', demoData?.summary?.ok_smoke_checks ?? '--');
    set('act-time-1', latest?.updated_at ? new Date(latest.updated_at).toLocaleString() : '—');
    set('mig-id-1', latest?.id || 'scan-loading');
    set('mig-source-1', latest?.src_account || '—');
    set('mig-target-1', latest?.src_region || '—');
    set('mig-date-1', latest?.created_at ? new Date(latest.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '—');
    const statusBadge = document.getElementById('mig-status-1');
    if (statusBadge && latest?.status) {
      statusBadge.textContent = latest.status;
      statusBadge.className = `badge ${latest.status === 'completed' ? 'badge-success' : 'badge-pending'}`;
    }

    const badge = document.getElementById('mig-badge');
    if (badge) {
      badge.textContent = jobs.length ? String(jobs.length) : '';
    }

    renderRows(jobs);
    if (latestSummary) {
      renderInventory(latestSummary);
    } else if (demoData?.inventory) {
      renderInventory(demoData.inventory);
    }
  } catch (error) {
    console.warn('Could not load live scans:', error.message);
    if (demoData?.inventory) {
      const counts = demoData.inventory.counts || {};
      set('stat-resources', demoData.summary?.created_resources ?? '--');
      set('stat-resources-delta', `${counts.lambda_functions || 0} Lambda · ${counts.sqs_queues || 0} SQS · ${counts.dynamodb_tables || 0} DDB`);
      set('stat-checks', demoData.summary?.ok_smoke_checks ?? '--');
      set('stat-checks-delta', `${demoData.summary?.ok_smoke_checks ?? '--'} passed`);
      set('stat-issues', demoData.summary?.validation_issue_checks ?? '--');
      set('stat-issues-delta', demoData.summary?.validation_issue_checks ? `${demoData.summary.validation_issue_checks} flagged` : 'No issues');
      set('mig-id-1', `demo-${demoData.inventory?.source_env || 'sandbox1'}`);
      set('mig-source-1', demoData.inventory?.source_env || 'sandbox1');
      set('mig-target-1', demoData.deployment_manifest?.target_env || 'sandbox2');
      set('mig-date-1', demoData.inventory?.region || '—');
      const statusBadge = document.getElementById('mig-status-1');
      if (statusBadge) {
        statusBadge.textContent = 'demo';
        statusBadge.className = 'badge badge-success';
      }
      renderInventory(demoData.inventory);
    }
  }
}

loadDashboard();
