const state = {
  currentJobId: null,
  pollTimer: null,
  runs: null,
};

const $ = (id) => document.getElementById(id);

function initTabs() {
  document.querySelectorAll('.nav-button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.nav-button').forEach((item) => item.classList.remove('active'));
      document.querySelectorAll('.tab').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      $(`tab-${button.dataset.tab}`).classList.add('active');
      if (button.dataset.tab === 'outputs') loadRuns();
      if (button.dataset.tab === 'jobs') loadJobs();
      if (button.dataset.tab === 'config') loadConfig();
    });
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response;
}

async function createJob(payload) {
  const response = await api('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const job = await response.json();
  state.currentJobId = job.id;
  renderLatestJob(job);
  startPolling(job.id);
  return job;
}

function startPolling(jobId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const job = await (await api(`/api/jobs/${jobId}`)).json();
      renderLatestJob(job);
      if (job.status === 'completed' || job.status === 'failed') {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        await loadRuns();
        await loadJobs();
        if (job.command === 'chart' && job.status === 'completed') showChartFromJob(job);
        if (job.command === 'agent' && job.status === 'completed') showAgentFromJob(job);
      }
    } catch (error) {
      renderLatestJob({ status: 'failed', command: 'unknown', error: error.message });
      clearInterval(state.pollTimer);
    }
  }, 1500);
}

function renderLatestJob(job) {
  const output = job.output_dir ? `<div>Output: <code>${job.run_kind}/${job.output_dir}</code></div>` : '';
  const error = job.error ? `<div class="danger">${escapeHtml(job.error)}</div>` : '';
  $('latestJob').className = 'job-card';
  $('latestJob').innerHTML = `
    <div><span class="badge ${job.status}">${job.status}</span> <strong>${job.command}</strong></div>
    <div class="hint">${job.started_at || job.created_at || ''}${job.finished_at ? ` → ${job.finished_at}` : ''}</div>
    ${output}${error}
    ${job.logs ? `<pre class="job-log">${escapeHtml(job.logs.slice(-5000))}</pre>` : ''}
  `;
}

async function loadJobs() {
  const jobs = await (await api('/api/jobs')).json();
  const box = $('jobsList');
  if (!jobs.length) {
    box.innerHTML = '<div class="panel muted">No jobs yet.</div>';
    return;
  }
  box.innerHTML = jobs.map((job) => `
    <section class="panel">
      <div><span class="badge ${job.status}">${job.status}</span> <strong>${job.command}</strong> <code>${job.id.slice(0, 8)}</code></div>
      <p>${job.started_at || job.created_at || ''}${job.finished_at ? ` → ${job.finished_at}` : ''}</p>
      ${job.output_dir ? `<p>Output: <code>${job.run_kind}/${job.output_dir}</code></p>` : ''}
      ${job.error ? `<p class="danger">${escapeHtml(job.error)}</p>` : ''}
      ${job.logs ? `<pre class="job-log">${escapeHtml(job.logs.slice(-8000))}</pre>` : ''}
    </section>`).join('');
}

async function loadRuns() {
  const runs = await (await api('/api/runs')).json();
  state.runs = runs;
  renderRunList('scanRuns', 'scans', runs.scans);
  renderRunList('backtestRuns', 'backtests', runs.backtests);
  renderRunList('chartRuns', 'charts', runs.charts);
  renderRunList('agentRuns', 'agents', runs.agents);
}

function renderRunList(containerId, kind, runs) {
  const container = $(containerId);
  if (!runs.length) {
    container.innerHTML = '<div class="muted">No runs yet.</div>';
    return;
  }
  container.innerHTML = runs.slice(0, 20).map((run) => `
    <div class="run-item">
      <h4>${run.id}</h4>
      <div class="file-row">
        ${run.files.map((file) => `<button class="file-pill" data-kind="${kind}" data-run="${run.id}" data-file="${file}">${file}</button>`).join('')}
      </div>
    </div>
  `).join('');
  container.querySelectorAll('.file-pill').forEach((button) => {
    button.addEventListener('click', () => previewFile(button.dataset.kind, button.dataset.run, button.dataset.file));
  });
}

async function previewFile(kind, runId, filename) {
  const fileUrl = `/api/files/${kind}/${runId}/${filename}`;
  $('downloadLink').href = fileUrl;
  $('downloadLink').textContent = `Open ${filename}`;
  if (/\.(png|jpg|jpeg|gif)$/i.test(filename)) {
    $('fileViewer').className = 'viewer';
    $('fileViewer').innerHTML = `<img src="${fileUrl}" alt="${escapeHtml(filename)}" />`;
    return;
  }
  const preview = await (await api(`/api/preview/${kind}/${runId}/${filename}`)).json();
  if (preview.type === 'csv') {
    $('fileViewer').className = 'viewer';
    $('fileViewer').innerHTML = csvTable(preview.rows);
  } else if (preview.type === 'text') {
    $('fileViewer').className = 'viewer';
    $('fileViewer').innerHTML = `<pre>${escapeHtml(preview.text)}</pre>`;
  } else {
    $('fileViewer').className = 'viewer empty';
    $('fileViewer').textContent = 'Binary file preview is unavailable.';
  }
}

function csvTable(rows) {
  if (!rows.length) return '<div class="empty">No rows.</div>';
  const columns = Object.keys(rows[0]);
  return `<div class="table-wrap"><table><thead><tr>${columns.map((col) => `<th>${escapeHtml(col)}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((col) => `<td>${renderCell(row[col] ?? '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

async function showChartFromJob(job) {
  if (!job.output_dir) return;
  const detail = await (await api(`/api/runs/charts/${job.output_dir}`)).json();
  const image = detail.files.find((file) => file.name.endsWith('_technical_chart.png'));
  const report = detail.files.find((file) => file.name === 'chart_report.md');
  $('chartPreview').className = 'panel preview';
  $('chartPreview').innerHTML = `
    ${image ? `<img src="${image.url}" alt="Generated chart" />` : ''}
    ${report ? `<p><a class="download" href="${report.url}" target="_blank">Open chart report</a></p>` : ''}
  `;
}

async function showAgentFromJob(job) {
  if (!job.output_dir) return;
  const detail = await (await api(`/api/runs/agents/${job.output_dir}`)).json();
  const report = detail.files.find((file) => file.name === 'agent_report.md');
  const sources = detail.files.find((file) => file.name.endsWith('_sources.csv'));
  $('agentPreview').className = 'panel preview';
  if (!report) {
    $('agentPreview').innerHTML = '<div class="empty">Agent finished, but no report file was found.</div>';
    return;
  }
  const preview = await (await api(`/api/preview/agents/${job.output_dir}/agent_report.md`)).json();
  $('agentPreview').innerHTML = `
    <div class="panel-title-row">
      <a class="download" href="${report.url}" target="_blank">Open agent report</a>
      ${sources ? `<a class="download" href="${sources.url}" target="_blank">Open sources</a>` : ''}
    </div>
    <pre>${escapeHtml(preview.text || '')}</pre>
  `;
}

async function loadConfig() {
  try {
    const text = await (await api('/api/config')).text();
    $('configEditor').value = text;
    $('configStatus').textContent = 'Config loaded.';
  } catch (error) {
    $('configStatus').textContent = `Could not load config: ${error.message}`;
  }
}

async function saveConfig() {
  try {
    await api('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: $('configEditor').value }),
    });
    $('configStatus').textContent = 'Saved config.yaml.';
  } catch (error) {
    $('configStatus').textContent = `Save failed: ${error.message}`;
  }
}


async function shutdownServer() {
  const confirmed = window.confirm('Stop the local market-signal-scanner GUI server? You will need to restart it from Terminal to use the GUI again.');
  if (!confirmed) return;
  const status = $('shutdownStatus');
  status.textContent = 'Shutdown requested...';
  try {
    await api('/api/shutdown', { method: 'POST' });
    status.textContent = 'Server stopped. You can close this tab.';
  } catch (error) {
    status.textContent = `Shutdown request failed: ${error.message}`;
  }
}

function wireActions() {
  $('runScan').addEventListener('click', () => createJob({ command: 'scan', skip_fundamentals: $('scanSkipFundamentals').checked }));
  $('runBacktest').addEventListener('click', () => createJob({ command: 'backtest' }));
  $('runChart').addEventListener('click', () => createJob({
    command: 'chart',
    ticker: $('chartTicker').value.trim(),
    period: $('chartPeriod').value.trim(),
    interval: $('chartInterval').value.trim(),
    chart_type: $('chartType').value,
    lookback: Number($('chartLookback').value || 180),
    moving_averages: $('chartMa').value.trim() || '20,50,100,200',
    no_support_resistance: $('hideSR').checked,
    no_bollinger: $('hideBollinger').checked,
    no_volume: $('hideVolume').checked,
    no_rsi: $('hideRsi').checked,
    no_macd: $('hideMacd').checked,
  }));
  $('runAgent').addEventListener('click', () => createJob({
    command: 'agent',
    ticker: $('agentTicker').value.trim(),
  }));
  $('refreshRuns').addEventListener('click', loadRuns);
  $('refreshJobs').addEventListener('click', loadJobs);
  $('refreshJobsPage').addEventListener('click', loadJobs);
  $('loadConfig').addEventListener('click', loadConfig);
  $('saveConfig').addEventListener('click', saveConfig);
  $('shutdownServer').addEventListener('click', shutdownServer);
}


function renderCell(value) {
  const text = String(value ?? '');
  if (/^https?:\/\//i.test(text)) {
    const label = linkLabel(text);
    return `<a class="table-link" href="${escapeHtml(text)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`;
  }
  return escapeHtml(text);
}

function linkLabel(url) {
  try {
    const parsed = new URL(url);
    if (parsed.hostname.includes('yahoo')) return 'Yahoo Finance';
    if (parsed.hostname.includes('google')) return 'Google Finance';
    if (parsed.hostname.includes('tradingview')) return 'TradingView';
    return parsed.hostname.replace(/^www\./, '');
  } catch (_) {
    return url;
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

initTabs();
wireActions();
loadRuns();
loadJobs();
loadConfig();
