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
      if (button.dataset.tab === 'llm') loadLlmStatus();
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
      <h4>${escapeHtml(formatRunTitle(run.id))}</h4>
      <div class="run-meta">${escapeHtml(formatRunSubtitle(run.id))}</div>
      <div class="file-row">
        ${run.files.map((file) => `<button class="file-pill" data-kind="${kind}" data-run="${run.id}" data-file="${file}">${file}</button>`).join('')}
      </div>
    </div>
  `).join('');
  container.querySelectorAll('.file-pill').forEach((button) => {
    button.addEventListener('click', () => previewFile(button.dataset.kind, button.dataset.run, button.dataset.file));
  });
}

function formatRunTitle(runId) {
  const parsed = parseRunId(runId);
  if (!parsed) return runId;
  const dateLabel = parsed.date.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
  const timeLabel = parsed.date.toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
  });
  return `${dateLabel} at ${timeLabel}${parsed.suffix ? ` - ${parsed.suffix}` : ''}`;
}

function formatRunSubtitle(runId) {
  const parsed = parseRunId(runId);
  return parsed ? runId : 'Output folder';
}

function parseRunId(runId) {
  const match = String(runId).match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})(?:_(.+))?$/);
  if (!match) return null;
  const [, year, month, day, hour, minute, second, suffix] = match;
  const date = new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
  );
  if (Number.isNaN(date.getTime())) return null;
  return { date, suffix: suffix || '' };
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
    $('fileViewer').innerHTML = filename.toLowerCase().endsWith('.md')
      ? renderMarkdown(preview.text)
      : `<pre>${escapeHtml(preview.text)}</pre>`;
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
    ${renderMarkdown(preview.text || '')}
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

async function loadLlmStatus() {
  const configBox = $('llmConfig');
  const statusBox = $('llmStatus');
  const modelsBox = $('llmModels');
  $('llmActionStatus').textContent = 'Checking LLM status...';
  try {
    const status = await (await api('/api/llm/status')).json();
    renderLlmStatus(status);
    $('llmActionStatus').textContent = `Status checked at ${new Date().toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' })}.`;
  } catch (error) {
    configBox.className = 'detail-list';
    configBox.innerHTML = `<div class="danger">${escapeHtml(error.message)}</div>`;
    statusBox.className = 'detail-list';
    statusBox.innerHTML = '<div class="muted">Could not load LLM status.</div>';
    modelsBox.className = 'model-list muted';
    modelsBox.textContent = 'No models loaded.';
  }
}

function renderLlmStatus(status) {
  $('llmConfig').className = 'detail-list';
  $('llmConfig').innerHTML = `
    <div><span>Provider</span><strong>${escapeHtml(status.provider)}</strong></div>
    <div><span>Model</span><strong>${escapeHtml(status.model)}</strong></div>
    <div><span>Server</span><code>${escapeHtml(status.base_url)}</code></div>
  `;

  const serverBadge = status.server_running ? '<span class="badge completed">running</span>' : '<span class="badge failed">not running</span>';
  const modelBadge = status.model_available ? '<span class="badge completed">available</span>' : '<span class="badge queued">missing</span>';
  const owner = llmProcessOwner(status);
  $('llmStatus').className = 'detail-list';
  $('llmStatus').innerHTML = `
    <div><span>Ollama server</span>${serverBadge}</div>
    <div><span>Configured model</span>${modelBadge}</div>
    <div><span>Process control</span><strong>${escapeHtml(owner)}</strong></div>
    ${status.error ? `<div><span>Last error</span><strong class="danger">${escapeHtml(status.error)}</strong></div>` : ''}
  `;

  $('startLlm').disabled = !status.can_start || status.server_running;
  $('stopLlm').disabled = !status.can_stop;
  $('startLlm').textContent = status.server_running ? 'Already Running' : 'Start Ollama';
  $('stopLlm').textContent = status.server_running && !status.can_stop ? 'External Process' : 'Stop Ollama';
  $('llmModels').className = 'model-list';
  if (status.installed_models && status.installed_models.length) {
    $('llmModels').innerHTML = status.installed_models.map((model) => {
      const selected = model === status.model ? ' selected' : '';
      return `<span class="model-pill${selected}">${escapeHtml(model)}</span>`;
    }).join('');
  } else if (status.server_running) {
    $('llmModels').innerHTML = '<span class="muted">Ollama is running, but no installed models were reported.</span>';
  } else {
    $('llmModels').innerHTML = `<span class="muted">Start Ollama, then install the configured model with <code>ollama pull ${escapeHtml(status.model)}</code>.</span>`;
  }

  renderLlmHelp(status);
}

function llmProcessOwner(status) {
  if (status.managed_by_app) return 'Started by this app';
  if (status.server_running) return 'Started outside this app';
  return 'Not running';
}

function renderLlmHelp(status) {
  const notes = [];
  if (status.server_running) {
    notes.push('Start is disabled because Ollama is already running.');
  }
  if (status.server_running && !status.can_stop) {
    notes.push('Stop is disabled because this app did not start the current Ollama process.');
  }
  if (!status.model_available) {
    notes.push(`Install the configured model with: ollama pull ${status.model}`);
  }
  if (!notes.length) {
    notes.push('The configured LLM is ready for Agent Research.');
  }
  $('llmHelp').innerHTML = notes.map((note) => `<div>${escapeHtml(note)}</div>`).join('');
}

async function startLlm() {
  await runLlmAction('/api/llm/start', 'Starting Ollama...');
}

async function stopLlm() {
  const confirmed = window.confirm('Stop the Ollama server started by this app?');
  if (!confirmed) return;
  await runLlmAction('/api/llm/stop', 'Stopping Ollama...');
}

async function runLlmAction(path, pendingMessage) {
  $('llmActionStatus').textContent = pendingMessage;
  try {
    const status = await (await api(path, { method: 'POST' })).json();
    renderLlmStatus(status);
    $('llmActionStatus').textContent = status.message || 'Done.';
  } catch (error) {
    $('llmActionStatus').textContent = error.message;
    await loadLlmStatus();
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
  $('refreshLlm').addEventListener('click', loadLlmStatus);
  $('startLlm').addEventListener('click', startLlm);
  $('stopLlm').addEventListener('click', stopLlm);
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

function renderMarkdown(markdown) {
  const lines = String(markdown || '').replace(/\r\n/g, '\n').split('\n');
  const html = [];
  let paragraph = [];
  let list = [];
  let inCode = false;
  let code = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    html.push(`<p>${inlineMarkdown(paragraph.join(' '))}</p>`);
    paragraph = [];
  };
  const flushList = () => {
    if (!list.length) return;
    html.push(`<ul>${list.map((item) => `<li>${inlineMarkdown(item)}</li>`).join('')}</ul>`);
    list = [];
  };
  const flushBlocks = () => {
    flushParagraph();
    flushList();
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.trim().startsWith('```')) {
      if (inCode) {
        html.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
        code = [];
        inCode = false;
      } else {
        flushBlocks();
        inCode = true;
      }
      continue;
    }

    if (!inCode && isMarkdownTableStart(lines, index)) {
      flushBlocks();
      const table = collectMarkdownTable(lines, index);
      html.push(renderMarkdownTable(table.rows, table.alignments));
      index = table.nextIndex - 1;
      continue;
    }

    if (inCode) {
      code.push(line);
      continue;
    }

    if (!line.trim()) {
      flushBlocks();
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      flushBlocks();
      const level = heading[1].length;
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    if (/^[-*_]{3,}\s*$/.test(line.trim())) {
      flushBlocks();
      html.push('<hr />');
      continue;
    }

    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      flushParagraph();
      list.push(bullet[1]);
      continue;
    }

    flushList();
    paragraph.push(line.trim());
  }

  if (inCode) html.push(`<pre><code>${escapeHtml(code.join('\n'))}</code></pre>`);
  flushBlocks();
  return `<article class="markdown-body">${html.join('')}</article>`;
}

function isMarkdownTableStart(lines, index) {
  if (index + 1 >= lines.length) return false;
  const header = lines[index].trim();
  const separator = lines[index + 1].trim();
  return isPipeRow(header) && isTableSeparator(separator) && splitTableRow(header).length === splitTableRow(separator).length;
}

function collectMarkdownTable(lines, startIndex) {
  const alignments = splitTableRow(lines[startIndex + 1]).map(tableAlignment);
  const rows = [splitTableRow(lines[startIndex])];
  let index = startIndex + 2;
  while (index < lines.length && isPipeRow(lines[index].trim())) {
    rows.push(splitTableRow(lines[index]));
    index += 1;
  }
  return { rows, alignments, nextIndex: index };
}

function renderMarkdownTable(rows, alignments) {
  if (!rows.length) return '';
  const header = rows[0];
  const body = rows.slice(1);
  const headerHtml = header.map((cell, index) => `<th${alignAttr(alignments[index])}>${inlineMarkdown(cell)}</th>`).join('');
  const bodyHtml = body.map((row) => {
    const cells = header.map((_cell, index) => row[index] || '');
    return `<tr>${cells.map((cell, index) => `<td${alignAttr(alignments[index])}>${inlineMarkdown(cell)}</td>`).join('')}</tr>`;
  }).join('');
  return `<div class="markdown-table-wrap"><table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`;
}

function isPipeRow(line) {
  return line.includes('|') && splitTableRow(line).length >= 2;
}

function isTableSeparator(line) {
  if (!isPipeRow(line)) return false;
  return splitTableRow(line).every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
}

function splitTableRow(line) {
  let clean = line.trim();
  if (clean.startsWith('|')) clean = clean.slice(1);
  if (clean.endsWith('|')) clean = clean.slice(0, -1);
  return clean.split('|').map((cell) => cell.trim());
}

function tableAlignment(value) {
  const cell = value.trim();
  if (cell.startsWith(':') && cell.endsWith(':')) return 'center';
  if (cell.endsWith(':')) return 'right';
  return 'left';
}

function alignAttr(alignment) {
  return alignment && alignment !== 'left' ? ` class="align-${alignment}"` : '';
}

function inlineMarkdown(value) {
  const parts = [];
  const linkPattern = /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;
  let lastIndex = 0;
  let match;
  while ((match = linkPattern.exec(String(value))) !== null) {
    parts.push(formatInlineText(String(value).slice(lastIndex, match.index)));
    parts.push(`<a href="${escapeHtml(match[2])}" target="_blank" rel="noopener noreferrer">${formatInlineText(match[1])}</a>`);
    lastIndex = match.index + match[0].length;
  }
  parts.push(formatInlineText(String(value).slice(lastIndex)));
  return parts.join('');
}

function formatInlineText(value) {
  let text = escapeHtml(value);
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
  text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  return text;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
}

initTabs();
wireActions();
loadRuns();
loadJobs();
loadConfig();
loadLlmStatus();
