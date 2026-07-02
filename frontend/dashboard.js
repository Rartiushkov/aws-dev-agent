const API = 'https://availabl-backend.onrender.com';

async function loadDashboard() {
  try {
    const res = await fetch(`${API}/summary`);
    const d = await res.json();

    // Stats
    const resources = (d.source?.lambda_count || 0) + (d.source?.sqs_count || 0) + (d.source?.dynamo_count || 0);
    document.getElementById('stat-resources').textContent = resources || '--';
    document.getElementById('stat-resources-delta').textContent = `${d.source?.lambda_count || 0} Lambda · ${d.source?.sqs_count || 0} SQS · ${d.source?.dynamo_count || 0} DDB`;

    const smoke = d.validation?.smoke_checks || 0;
    const issues = d.validation?.checks_with_issues || 0;
    document.getElementById('stat-checks').textContent = smoke || '--';
    document.getElementById('stat-checks-delta').textContent = smoke ? `${smoke - issues} passed` : 'Loading...';
    document.getElementById('stat-issues').textContent = issues !== undefined ? issues : '--';
    document.getElementById('stat-issues-delta').textContent = issues === 0 ? '✓ No critical issues' : `${issues} flagged`;

    // Migration row
    if (d.source?.account_id) {
      document.getElementById('mig-id-1').textContent = `mig-${d.source.account_id.slice(-6)}`;
    }
    if (d.source?.region) {
      document.getElementById('mig-source-1').textContent = d.source.region;
    }
    if (d.target?.account_id) {
      document.getElementById('mig-target-1').textContent = `acct-${d.target.account_id.slice(-6)}`;
    }

    // Activity
    document.getElementById('act-smoke').textContent = smoke || '--';
    document.getElementById('act-time-1').textContent = d.source?.region ? `Region: ${d.source.region}` : 'Today';

    // Inventory
    document.getElementById('inv-lambda').textContent = d.source?.lambda_count ?? '--';
    document.getElementById('inv-sqs').textContent = d.source?.sqs_count ?? '--';
    document.getElementById('inv-dynamo').textContent = d.source?.dynamo_count ?? '--';
    document.getElementById('inv-deps').textContent = d.source?.dependency_node_count ?? '--';

  } catch (e) {
    console.warn('Backend unavailable, showing static data');
  }
}

loadDashboard();
