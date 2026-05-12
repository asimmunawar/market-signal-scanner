const state = {
  currentTab: 'home',
  currentJobId: null,
  pollTimer: null,
  agentSessionId: null,
  agentPollTimer: null,
  trendCatcherSessionId: null,
  trendCatcherPollTimer: null,
  activityPollTimer: null,
  runs: null,
  chartData: null,
  chartView: { start: 0, end: 0 },
  chartDrag: null,
  chartSelection: null,
  chartHover: null,
  chartTickerChoices: null,
  tickerDiscoveryMode: 'quick',
  tickerDiscoveryCandidates: [],
  opportunityData: null,
  opportunityHover: null,
  opportunityPoints: [],
  opportunitySelected: null,
  guardrailsData: null,
  guardrailsSelected: null,
};

const $ = (id) => document.getElementById(id);
const CHART_COLORS = {
  sma: ['#2563eb', '#d946ef', '#f97316', '#64748b', '#0f766e', '#b91c1c'],
  ema20: '#14b8a6',
  ema50: '#a855f7',
  bollinger: '#94a3b8',
  support: '#16a34a',
  resistance: '#dc2626',
  trendSupport: '#15803d',
  trendResistance: '#b91c1c',
};

window.addEventListener('error', (event) => {
  const status = $('newsStatus');
  if (status) status.textContent = `Browser error: ${event.message}`;
});

window.addEventListener('unhandledrejection', (event) => {
  const status = $('newsStatus');
  const message = event.reason && event.reason.message ? event.reason.message : String(event.reason || 'Unknown promise error');
  if (status) status.textContent = `Request error: ${message}`;
});

function initTabs() {
  document.querySelectorAll('.nav-button').forEach((button) => {
    button.addEventListener('click', () => {
      activateTab(button.dataset.tab, { updateLocation: true });
    });
  });
  window.addEventListener('hashchange', () => {
    activateTab(tabFromLocation(), { updateLocation: false });
  });
  activateTab(tabFromLocation(), { updateLocation: false });
}

function tabFromLocation() {
  const hashTab = window.location.hash.replace(/^#/, '').trim();
  if (isKnownTab(hashTab)) return hashTab;
  return 'home';
}

function isKnownTab(tab) {
  return Boolean(tab && $(`tab-${tab}`) && document.querySelector(`.nav-button[data-tab="${tab}"]`));
}

function activateTab(tab, options = {}) {
  const selected = isKnownTab(tab) ? tab : 'run';
  state.currentTab = selected;
  document.querySelectorAll('.nav-button').forEach((item) => {
    item.classList.toggle('active', item.dataset.tab === selected);
  });
  document.querySelectorAll('.tab').forEach((item) => {
    item.classList.toggle('active', item.id === `tab-${selected}`);
  });
  localStorage.setItem('marketSignalScanner.activeTab', selected);
  if (options.updateLocation && window.location.hash !== `#${selected}`) {
    history.replaceState(null, '', `#${selected}`);
  }
  loadTabData(selected);
}

function loadTabData(tab) {
  if (tab === 'run') loadRunProgress();
  if (tab === 'outputs') loadRuns();
  if (tab === 'activity') loadActivity();
  if (tab === 'config') loadConfig();
  if (tab === 'llm') loadLlmStatus();
  if (tab === 'chart') ensureInteractiveChart();
  if (tab === 'opportunity') ensureOpportunityMap();
  if (tab === 'guardrails') ensureGuardrails();
  if (tab === 'agent') {
    loadAgentSuggestions();
    restoreAgentSession();
  }
  if (tab === 'trend-catcher') restoreTrendCatcherSession();
}

function wireTabLinks() {
  document.querySelectorAll('[data-go-tab]').forEach((button) => {
    button.addEventListener('click', () => {
      activateTab(button.dataset.goTab, { updateLocation: true });
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

function refreshThemeStylesheet() {
  const themeLink = document.querySelector('link[href^="/api/ui/theme.css"]');
  if (!themeLink) return;
  themeLink.href = `/api/ui/theme.css?v=${Date.now()}`;
}

async function loadAgentSuggestions() {
  const box = $('agentSuggestions');
  if (!box) return;
  try {
    const data = await (await api('/api/agent/suggested-questions')).json();
    renderAgentSuggestions(data.questions || []);
  } catch (error) {
    box.innerHTML = '';
  }
}

function renderAgentSuggestions(questions) {
  const box = $('agentSuggestions');
  if (!box) return;
  box.innerHTML = questions.map((question) => (
    `<button class="agent-suggestion-button" type="button" data-question="${escapeHtml(question)}">${escapeHtml(question)}</button>`
  )).join('');
}

function useAgentSuggestion(question) {
  const input = $('agentQuestion');
  input.value = question;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function openTickerChart(ticker) {
  $('chartTicker').value = ticker;
  syncChartTickerSelect(ticker);
  activateTab('chart', { updateLocation: true });
  loadInteractiveChart();
}

function runTickerNews(ticker) {
  $('newsTicker').value = ticker;
  activateTab('news', { updateLocation: true });
  runNewsFromUi();
}

async function startDecisionAgent(ticker) {
  const query = decisionResearchPrompt(ticker);
  activateTab('agent', { updateLocation: true });
  $('agentQuestion').value = query;
  await startAgentFromQuestion(query, ticker);
}

function decisionResearchPrompt(ticker) {
  return `I am a small long-term investor reviewing ${ticker}. Give me a concise decision-grade research brief before I buy, hold, trim, or avoid it. Separate the answer into: 1) technical analysis, 2) fundamental analysis, 3) recent news and catalysts with source dates, 4) FOMO/chase risk, 5) sell-review risks, 6) what I should verify before making a final decision. Do not give certainty or financial advice.`;
}

async function ensureOpportunityMap() {
  if (!state.opportunityData) await loadOpportunityMap();
  else drawOpportunityMap();
}

async function loadOpportunityMap() {
  const status = $('opportunityStatus');
  if (status) status.textContent = 'Loading latest scan...';
  try {
    const data = await (await api('/api/opportunity-map')).json();
    state.opportunityData = data;
    state.opportunitySelected = null;
    renderOpportunitySummary();
    renderOpportunityBoards();
    selectDefaultOpportunity();
    drawOpportunityMap();
    const shown = filteredOpportunityRows().length;
    if (status) status.textContent = `Latest scan ${data.run_id}: showing ${shown} of ${data.row_count} tickers.`;
  } catch (error) {
    if (status) status.textContent = `Could not load opportunity map: ${error.message}`;
    $('opportunitySummary').innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
    $('opportunityLeaderboard').innerHTML = '<div class="muted">Run a scan first.</div>';
    $('opportunityHeatmap').innerHTML = '<div class="muted">Run a scan first.</div>';
  }
}

function filteredOpportunityRows() {
  const rows = state.opportunityData?.rows || [];
  const assetType = $('opportunityAssetType')?.value || 'all';
  const recommendation = $('opportunityRecommendation')?.value || 'all';
  const minScore = Number($('opportunityMinScore')?.value || -100);
  return rows.filter((row) => (
    (assetType === 'all' || row.asset_type === assetType)
    && (recommendation === 'all' || row.recommendation === recommendation)
    && Number(row.score ?? -999) >= minScore
  ));
}

function renderOpportunitySummary() {
  const box = $('opportunitySummary');
  if (!box || !state.opportunityData) return;
  const rows = state.opportunityData.rows || [];
  const summary = state.opportunityData.summary || {};
  const recs = summary.recommendations || {};
  const quadrants = summary.quadrants || {};
  const averageScore = rows.length ? rows.reduce((sum, row) => sum + Number(row.score || 0), 0) / rows.length : 0;
  const attractive = quadrants.Attractive || 0;
  const speculative = quadrants.Speculative || 0;
  box.innerHTML = [
    opportunityStat('Universe', rows.length, 'tickers', 'How many tickers were included in the latest scan.'),
    opportunityStat('Avg Score', averageScore.toFixed(1), 'scanner', 'Average scanner score across the universe. Positive is generally stronger; negative is weaker.'),
    opportunityStat('Strong Buy / Buy', `${recs['Strong Buy'] || 0} / ${recs.Buy || 0}`, 'ranked', 'How many names currently rank in the top two scanner recommendation buckets.'),
    opportunityStat('Attractive', attractive, 'lower-risk high score', 'Higher score with lower risk. These are research candidates, not automatic buys.'),
    opportunityStat('Speculative', speculative, 'higher-risk high score', 'High score but also high risk. These deserve smaller sizing and more patience.'),
  ].join('');
}

function opportunityStat(label, value, note, help = '') {
  const helpHtml = help ? ` ${helpTip(help)}` : '';
  return `<div class="opportunity-stat"><span>${escapeHtml(label)}${helpHtml}</span><strong>${escapeHtml(String(value))}</strong><em>${escapeHtml(note)}</em></div>`;
}

function renderOpportunityBoards() {
  const rows = filteredOpportunityRows();
  renderOpportunityLeaderboard(rows);
  renderOpportunityHeatmap(rows);
  if (!rows.some((row) => row.ticker === state.opportunitySelected)) {
    state.opportunitySelected = null;
    const detail = $('opportunityDetail');
    if (detail) detail.innerHTML = '<div class="muted">Click a bubble or leaderboard row to inspect a ticker.</div>';
  }
  selectDefaultOpportunity();
  const status = $('opportunityStatus');
  if (status && state.opportunityData) status.textContent = `Latest scan ${state.opportunityData.run_id}: showing ${rows.length} of ${state.opportunityData.row_count} tickers.`;
}

function selectDefaultOpportunity() {
  if (state.opportunitySelected) return;
  const rows = filteredOpportunityRows();
  const defaultRow = rows
    .slice()
    .sort((a, b) => Number(b.opportunity ?? b.score ?? -999) - Number(a.opportunity ?? a.score ?? -999))[0];
  if (defaultRow) {
    state.opportunitySelected = defaultRow.ticker;
    renderOpportunityDetail(defaultRow);
  }
}

function renderOpportunityLeaderboard(rows) {
  const box = $('opportunityLeaderboard');
  if (!box) return;
  const ranked = rows.slice().sort((a, b) => Number(b.opportunity ?? b.score ?? -999) - Number(a.opportunity ?? a.score ?? -999)).slice(0, 18);
  if (!ranked.length) {
    box.innerHTML = '<div class="muted">No tickers match the filters.</div>';
    return;
  }
  box.innerHTML = ranked.map((row) => `
    <button class="opportunity-row" type="button" data-ticker="${escapeHtml(row.ticker)}">
      <span>
        <strong>${escapeHtml(row.ticker)}</strong>
        <em>${escapeHtml(row.asset_type)} · ${escapeHtml(row.quadrant)}</em>
      </span>
      <span class="rec-pill ${recommendationClass(row.recommendation)}">${escapeHtml(row.recommendation)}</span>
      <span class="opportunity-score">${numberText(row.score, 1)}</span>
    </button>
  `).join('');
}

function renderOpportunityHeatmap(rows) {
  const box = $('opportunityHeatmap');
  if (!box) return;
  const ranked = rows.slice().sort((a, b) => Number(b.score ?? -999) - Number(a.score ?? -999)).slice(0, 24);
  if (!ranked.length) {
    box.innerHTML = '<div class="muted">No tickers match the filters.</div>';
    return;
  }
  const columns = [
    ['1D', 'return_1d', 'return'],
    ['5D', 'return_5d', 'return'],
    ['1M', 'return_1m', 'return'],
    ['3M', 'return_3m', 'return'],
    ['6M', 'return_6m', 'return'],
    ['1Y', 'return_1y', 'return'],
    ['RSI', 'rsi_14', 'rsi'],
    ['Score', 'score', 'score'],
  ];
  box.innerHTML = `
    <div class="heatmap-grid" style="--heat-cols:${columns.length}">
      <div class="heat-head">Ticker</div>
      ${columns.map(([label]) => `<div class="heat-head">${label}</div>`).join('')}
      ${ranked.map((row) => `
        <button class="heat-ticker" type="button" data-ticker="${escapeHtml(row.ticker)}">${escapeHtml(row.ticker)}</button>
        ${columns.map(([, key, type]) => `<button class="heat-cell" type="button" data-ticker="${escapeHtml(row.ticker)}" style="${heatStyle(row[key], type)}">${heatValue(row[key], type)}</button>`).join('')}
      `).join('')}
    </div>
  `;
}

function drawOpportunityMap() {
  const canvas = $('opportunityCanvas');
  if (!canvas || !state.opportunityData) return;
  const tooltip = $('opportunityTooltip');
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(760, Math.floor(rect.width || 1100));
  const height = Math.max(560, Math.floor(rect.height || 620));
  if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = cssVar('--panel', '#ffffff');
  ctx.fillRect(0, 0, width, height);
  const rows = filteredOpportunityRows().filter((row) => isFiniteNumber(row[$('opportunityXAxis').value]) && isFiniteNumber(row[$('opportunityYAxis').value]));
  const inner = { x: 64, y: 28, w: width - 104, h: height - 86 };
  const xKey = $('opportunityXAxis').value;
  const yKey = $('opportunityYAxis').value;
  const xValues = rows.map((row) => xMetricValue(row, xKey));
  const yValues = rows.map((row) => yMetricValue(row, yKey));
  const xScale = opportunityScale(xValues, inner.x, inner.w, 0.08);
  const yScale = opportunityScale(yValues, inner.y + inner.h, -inner.h, 0.08);
  drawOpportunityFrame(ctx, inner, xScale, yScale, axisLabel(xKey), axisLabel(yKey));
  drawOpportunityQuadrants(ctx, inner, xScale, yScale, xKey, yKey);
  state.opportunityPoints = rows.map((row) => {
    const cap = Number(row.market_cap || 0);
    const volume = Number(row.avg_volume_20d || 0);
    const sizeBase = cap > 0 ? Math.log10(cap) : Math.log10(Math.max(10, volume));
    const radius = Math.max(5, Math.min(18, (sizeBase - 5) * 2.2));
    return {
      row,
      x: xScale.to(xMetricValue(row, xKey)),
      y: yScale.to(yMetricValue(row, yKey)),
      r: radius,
    };
  });
  state.opportunityPoints.forEach((point) => drawOpportunityBubble(ctx, point, point.row.ticker === state.opportunitySelected));
  if (state.opportunityHover) drawOpportunityTooltip(tooltip);
  else if (tooltip) tooltip.style.display = 'none';
}

function drawOpportunityFrame(ctx, inner, xScale, yScale, xLabel, yLabel) {
  ctx.strokeStyle = cssVar('--line', '#d9e0e7');
  ctx.strokeRect(inner.x, inner.y, inner.w, inner.h);
  ctx.font = '11px ui-sans-serif, system-ui';
  ctx.fillStyle = cssVar('--muted', '#667085');
  for (let i = 0; i <= 4; i += 1) {
    const x = inner.x + (inner.w / 4) * i;
    const y = inner.y + (inner.h / 4) * i;
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.22)';
    ctx.beginPath();
    ctx.moveTo(x, inner.y);
    ctx.lineTo(x, inner.y + inner.h);
    ctx.moveTo(inner.x, y);
    ctx.lineTo(inner.x + inner.w, y);
    ctx.stroke();
    ctx.fillStyle = cssVar('--muted', '#667085');
    ctx.fillText(compactAxis(xScale.from(x)), x - 12, inner.y + inner.h + 20);
    ctx.fillText(compactAxis(yScale.from(y)), 10, y + 4);
  }
  ctx.fillStyle = cssVar('--ink', '#17212b');
  ctx.font = '700 13px ui-sans-serif, system-ui';
  ctx.fillText(xLabel, inner.x + inner.w - 150, inner.y + inner.h + 42);
  ctx.save();
  ctx.translate(18, inner.y + 126);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(yLabel, 0, 0);
  ctx.restore();
}

function drawOpportunityQuadrants(ctx, inner, xScale, yScale, xKey, yKey) {
  if (xKey !== 'risk' || yKey !== 'score') return;
  const riskLine = xScale.to(45);
  const scoreLine = yScale.to(45);
  ctx.save();
  ctx.strokeStyle = 'rgba(15, 23, 42, 0.28)';
  ctx.setLineDash([6, 6]);
  ctx.beginPath();
  ctx.moveTo(riskLine, inner.y);
  ctx.lineTo(riskLine, inner.y + inner.h);
  ctx.moveTo(inner.x, scoreLine);
  ctx.lineTo(inner.x + inner.w, scoreLine);
  ctx.stroke();
  ctx.setLineDash([]);
  opportunityZone(ctx, inner.x + 12, inner.y + 22, 'Attractive', '#166534');
  opportunityZone(ctx, riskLine + 12, inner.y + 22, 'Speculative', '#92400e');
  opportunityZone(ctx, inner.x + 12, scoreLine + 24, 'Watch', '#475569');
  opportunityZone(ctx, riskLine + 12, scoreLine + 24, 'Avoid', '#991b1b');
  ctx.restore();
}

function opportunityZone(ctx, x, y, label, color) {
  ctx.fillStyle = color;
  ctx.font = '800 12px ui-sans-serif, system-ui';
  ctx.fillText(label, x, y);
}

function drawOpportunityBubble(ctx, point, selected) {
  const color = recommendationColor(point.row.recommendation);
  ctx.save();
  ctx.globalAlpha = selected ? 0.98 : 0.78;
  ctx.fillStyle = color;
  ctx.strokeStyle = selected ? '#111827' : 'rgba(255,255,255,0.9)';
  ctx.lineWidth = selected ? 3 : 1.5;
  ctx.beginPath();
  ctx.arc(point.x, point.y, point.r, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  if (point.r >= 8 || selected) {
    ctx.fillStyle = '#111827';
    ctx.font = selected ? '800 12px ui-sans-serif, system-ui' : '700 10px ui-sans-serif, system-ui';
    ctx.fillText(point.row.ticker, point.x + point.r + 3, point.y + 4);
  }
  ctx.restore();
}

function drawOpportunityTooltip(tooltip) {
  if (!tooltip || !state.opportunityHover) return;
  const row = state.opportunityHover.row;
  tooltip.style.display = 'block';
  tooltip.style.left = `${state.opportunityHover.x + 14}px`;
  tooltip.style.top = `${state.opportunityHover.y + 14}px`;
  tooltip.innerHTML = `
    <strong>${escapeHtml(row.ticker)} · ${escapeHtml(row.recommendation)}</strong>
    <span>Score ${numberText(row.score, 1)} · Risk ${numberText(row.risk, 1)}</span>
    <span>1M ${pctText(row.return_1m)} · 3M ${pctText(row.return_3m)}</span>
    <span>RSI ${numberText(row.rsi_14, 1)} · ${escapeHtml(row.quadrant)}</span>
  `;
}

function selectOpportunityTicker(ticker) {
  const row = (state.opportunityData?.rows || []).find((item) => item.ticker === ticker);
  if (!row) return;
  state.opportunitySelected = row.ticker;
  renderOpportunityDetail(row);
  drawOpportunityMap();
}

function renderOpportunityDetail(row) {
  const box = $('opportunityDetail');
  if (!box) return;
  box.innerHTML = `
    <div class="opportunity-detail-head">
      <span class="asset-chip">${escapeHtml(row.asset_type)}</span>
      <span class="rec-pill ${recommendationClass(row.recommendation)}">${escapeHtml(row.recommendation)}</span>
    </div>
    <h3>${escapeHtml(row.ticker)}</h3>
    <p>${escapeHtml(row.name || row.ticker)}</p>
    ${tickerActionBar(row.ticker)}
    <div class="opportunity-metrics">
      ${metricTile('Score', numberText(row.score, 1), 'Overall scanner rating from -100 to +100. Higher means stronger combined signals.')}
      ${metricTile('Quality', numberText(row.opportunity, 1), 'Score adjusted down for excessive risk and overbought readings.')}
      ${metricTile('Risk', numberText(row.risk, 1), 'Composite of volatility and drawdown. Higher means bumpier and potentially harder to hold.')}
      ${metricTile('RSI', numberText(row.rsi_14, 1), 'Momentum gauge. Above 70 can mean overbought; below 30 can mean oversold.')}
      ${metricTile('1M', pctText(row.return_1m), 'Price return over roughly the last month.')}
      ${metricTile('1Y', pctText(row.return_1y), 'Price return over roughly the last year.')}
    </div>
    ${analysisGroup('Technical Analysis', 'Price, chart, momentum, volume, and risk behavior. Useful for timing and risk awareness, but not a business thesis.', [
      ['Trend / Momentum', `${numberText(row.trend_score, 1)} / ${numberText(row.momentum_score, 1)}`, 'Trend checks moving averages and price direction. Momentum checks RSI, MACD, and rate of change.'],
      ['RSI', numberText(row.rsi_14, 1), 'Momentum gauge. Above 70 can mean overbought; below 30 can mean oversold.'],
      ['Max Drawdown', pctText(row.max_drawdown), 'Largest peak-to-trough decline in the measured period.'],
      ['Volatility', pctText(row.volatility_annual), 'How much price tends to move around. Higher volatility can be harder to hold.'],
      ['Volume Spike', `${numberText(row.volume_spike, 2)}x`, 'Recent volume compared with normal volume. Spikes can indicate attention or news.'],
      ['Quadrant', row.quadrant, 'The map zone based on score and risk.'],
    ])}
    ${analysisGroup('Fundamental Analysis', 'Business, valuation, growth, profitability, balance sheet, and shareholder return. This is closer to the long-term thesis.', [
      ['Forward P/E', numberText(row.forward_pe, 1), 'Expected price/earnings ratio. Lower is cheaper only if quality and growth are acceptable.'],
      ['PEG', numberText(row.peg_ratio, 2), 'Valuation relative to growth. Lower can be better, but data can be noisy.'],
      ['Price/Book', numberText(row.price_to_book, 2), 'Price compared with accounting book value. More useful for banks than software firms.'],
      ['Revenue Growth', pctText(row.revenue_growth), 'Recent revenue growth where available.'],
      ['Profit Margin', pctText(row.profit_margin), 'How much revenue becomes profit. Higher margins can signal quality.'],
      ['Dividend Yield', pctText(row.dividend_yield), 'Cash dividend yield. Not all good long-term investments pay dividends.'],
    ])}
    ${analysisGroup('News / Research Next Step', 'News is not embedded in this scanner score. Use News Summary or Agent to check what changed recently before deciding.', [
      ['Recommended action', 'Run Agent or News Summary', 'This checks fresh sources and helps explain what the numbers alone cannot.'],
    ])}
    <div class="opportunity-links">
      ${row.yahoo_finance_url ? `<a href="${escapeHtml(row.yahoo_finance_url)}" target="_blank" rel="noopener noreferrer">Yahoo</a>` : ''}
      ${row.tradingview_url ? `<a href="${escapeHtml(row.tradingview_url)}" target="_blank" rel="noopener noreferrer">TradingView</a>` : ''}
    </div>
  `;
}

function metricTile(label, value, help = '') {
  return `<div><span>${escapeHtml(label)}${help ? ` ${helpTip(help)}` : ''}</span><strong>${escapeHtml(String(value))}</strong></div>`;
}

function tickerActionBar(ticker) {
  return `
    <div class="ticker-actions">
      <button class="ghost small" type="button" data-open-chart="${escapeHtml(ticker)}">Open Chart</button>
      <button class="ghost small" type="button" data-run-news="${escapeHtml(ticker)}">Run News Summary</button>
      <button class="primary small" type="button" data-agent-research="${escapeHtml(ticker)}">Ask Agent Before Deciding</button>
    </div>
  `;
}

function analysisGroup(title, subtitle, rows) {
  return `
    <div class="analysis-group">
      <h4>${escapeHtml(title)} ${helpTip(subtitle)}</h4>
      <p>${escapeHtml(subtitle)}</p>
      <div class="detail-list">
        ${rows.map(([label, value, help]) => `<div><span>${escapeHtml(label)} ${helpTip(help)}</span><strong>${escapeHtml(String(value || 'n/a'))}</strong></div>`).join('')}
      </div>
    </div>
  `;
}

async function ensureGuardrails() {
  if (!state.guardrailsData) await loadGuardrails();
  else renderGuardrails();
}

async function loadGuardrails() {
  const summary = $('guardrailSummary');
  if (summary) summary.innerHTML = '<div class="muted">Loading guardrails...</div>';
  try {
    const data = await (await api('/api/investor-guardrails')).json();
    state.guardrailsData = data;
    state.guardrailsSelected = null;
    renderGuardrails();
  } catch (error) {
    if (summary) summary.innerHTML = `<div class="muted">${escapeHtml(error.message)}</div>`;
    ['guardrailResearch', 'guardrailFomo', 'guardrailSellReview', 'guardrailSleep'].forEach((id) => {
      $(id).innerHTML = '<div class="muted">Run a scan first.</div>';
    });
  }
}

function renderGuardrails() {
  if (!state.guardrailsData) return;
  renderGuardrailSummary();
  renderGuardrailPreferenceReadout();
  renderGuardrailList('guardrailResearch', state.guardrailsData.research || [], 'research');
  renderGuardrailList('guardrailFomo', state.guardrailsData.fomo || [], 'fomo');
  renderGuardrailList('guardrailSellReview', state.guardrailsData.sell_review || [], 'sell_review');
  renderGuardrailList('guardrailSleep', state.guardrailsData.sleep_on_it || [], 'sleep_on_it');
  renderGuardrailSizing();
  const first = state.guardrailsSelected || (state.guardrailsData.research || [])[0] || (state.guardrailsData.fomo || [])[0];
  if (first) renderGuardrailDetail(first);
}

function renderGuardrailSummary() {
  const box = $('guardrailSummary');
  const data = state.guardrailsData;
  const summary = data.summary || {};
  box.innerHTML = [
    opportunityStat('Latest Scan', data.run_id, `${data.row_count} tickers`, 'The scan run used to build these guardrails.'),
    opportunityStat('Research', summary.research_count || 0, 'calmer candidates', 'Tickers strong enough to research without obvious chase warnings.'),
    opportunityStat('FOMO', summary.fomo_count || 0, 'slow down', 'Fast-moving or crowded names where emotion can overpower discipline.'),
    opportunityStat('Sell Review', summary.sell_review_count || 0, 'check thesis', 'Potential weak holdings to review. This does not mean automatic sell.'),
    opportunityStat('Sleep On It', summary.sleep_on_it_count || 0, 'high excitement/risk', 'Names where waiting before action may prevent a bad impulse decision.'),
  ].join('');
}

function renderGuardrailList(containerId, items, mode) {
  const box = $(containerId);
  if (!box) return;
  if (!items.length) {
    box.innerHTML = '<div class="muted">Nothing urgent in this bucket.</div>';
    return;
  }
  box.innerHTML = items.slice(0, 12).map((item) => `
    <button class="guardrail-item" type="button" data-ticker="${escapeHtml(item.ticker)}" data-mode="${escapeHtml(mode)}">
      <span>
        <strong>${escapeHtml(item.ticker)}</strong>
        <em>${escapeHtml(item.posture)}</em>
      </span>
      <span class="guardrail-mini">
        <b>${numberText(item.score, 1)}</b>
        <small>${escapeHtml(item.recommendation || '')}</small>
      </span>
    </button>
  `).join('');
}

function renderGuardrailSizing() {
  const box = $('guardrailSizing');
  if (!box || !state.guardrailsData) return;
  const budget = Math.max(0, Number($('guardrailBudget').value || 0));
  const prefs = guardrailPreferences();
  renderGuardrailPreferenceReadout();
  const ideas = (state.guardrailsData.research || []).slice(0, 5);
  if (!ideas.length || !budget) {
    box.innerHTML = '<div class="muted">No sizing ideas available yet.</div>';
    return;
  }
  const totalWeight = ideas.reduce((sum, item) => sum + guardrailSizingWeight(item), 0) || 1;
  box.innerHTML = ideas.map((item) => {
    const raw = budget * (guardrailSizingWeight(item) / totalWeight) * prefs.sizeMultiplier;
    const capped = Math.min(raw, budget * (prefs.maxIdeaPct / 100));
    const finalStarter = Math.max(5, Math.round(capped / 5) * 5);
    return `<div><strong>${escapeHtml(item.ticker)}</strong><span>${money(finalStarter)} starter · score ${numberText(item.score, 1)} · risk ${numberText(item.risk, 1)}</span></div>`;
  }).join('');
}

function guardrailSizingWeight(item) {
  const score = Math.max(0, Number(item.score || 0));
  const riskDrag = Math.max(0.35, 1 - Math.max(0, Number(item.risk || 0) - 25) / 120);
  return Math.max(1, score * riskDrag);
}

function guardrailPreferences() {
  const riskTolerance = clampNumber($('riskToleranceSlider')?.value, 1, 10, 5);
  const timeHorizon = clampNumber($('timeHorizonSlider')?.value, 1, 10, 7);
  const fomoBrake = clampNumber($('fomoBrakeSlider')?.value, 1, 10, 7);
  const maxIdeaPct = clampNumber($('maxIdeaPctSlider')?.value, 5, 60, 25);
  const riskMultiplier = 0.45 + riskTolerance * 0.07;
  const horizonMultiplier = 0.72 + timeHorizon * 0.04;
  const fomoMultiplier = 1.12 - fomoBrake * 0.055;
  return {
    riskTolerance,
    timeHorizon,
    fomoBrake,
    maxIdeaPct,
    sizeMultiplier: Math.max(0.25, Math.min(1.25, riskMultiplier * horizonMultiplier * fomoMultiplier)),
  };
}

function renderGuardrailPreferenceReadout() {
  const prefs = guardrailPreferences();
  setTextIfPresent('riskToleranceValue', String(prefs.riskTolerance));
  setTextIfPresent('timeHorizonValue', String(prefs.timeHorizon));
  setTextIfPresent('fomoBrakeValue', String(prefs.fomoBrake));
  setTextIfPresent('maxIdeaPctValue', `${prefs.maxIdeaPct}%`);
  const riskText = prefs.riskTolerance <= 3 ? 'cautious' : prefs.riskTolerance >= 8 ? 'risk-tolerant' : 'balanced';
  const horizonText = prefs.timeHorizon <= 3 ? 'shorter-term' : prefs.timeHorizon >= 8 ? 'long-term' : 'medium-term';
  const fomoText = prefs.fomoBrake >= 8 ? 'strict anti-FOMO brake' : prefs.fomoBrake <= 3 ? 'light anti-FOMO brake' : 'moderate anti-FOMO brake';
  const readout = $('preferenceReadout');
  if (readout) {
    readout.innerHTML = `Profile: <strong>${riskText}</strong>, <strong>${horizonText}</strong>, with a <strong>${fomoText}</strong>. Starter ideas are capped at <strong>${prefs.maxIdeaPct}%</strong> of this budget.`;
  }
}

function setTextIfPresent(id, text) {
  const element = $(id);
  if (element) element.textContent = text;
}

function clampNumber(value, min, max, fallback) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return fallback;
  return Math.max(min, Math.min(max, numeric));
}

function saveGuardrailPreferences() {
  const prefs = guardrailPreferences();
  localStorage.setItem('marketSignalScanner.guardrailPreferences', JSON.stringify({
    riskTolerance: prefs.riskTolerance,
    timeHorizon: prefs.timeHorizon,
    fomoBrake: prefs.fomoBrake,
    maxIdeaPct: prefs.maxIdeaPct,
  }));
}

function loadGuardrailPreferences() {
  let prefs = {};
  try {
    prefs = JSON.parse(localStorage.getItem('marketSignalScanner.guardrailPreferences') || '{}');
  } catch {
    prefs = {};
  }
  const defaults = { riskTolerance: 5, timeHorizon: 7, fomoBrake: 7, maxIdeaPct: 25 };
  [
    ['riskToleranceSlider', prefs.riskTolerance ?? defaults.riskTolerance],
    ['timeHorizonSlider', prefs.timeHorizon ?? defaults.timeHorizon],
    ['fomoBrakeSlider', prefs.fomoBrake ?? defaults.fomoBrake],
    ['maxIdeaPctSlider', prefs.maxIdeaPct ?? defaults.maxIdeaPct],
  ].forEach(([id, value]) => {
    const element = $(id);
    if (element) element.value = value;
  });
  renderGuardrailPreferenceReadout();
}

function resetGuardrailPreferences() {
  localStorage.removeItem('marketSignalScanner.guardrailPreferences');
  ['riskToleranceSlider', 'timeHorizonSlider', 'fomoBrakeSlider', 'maxIdeaPctSlider'].forEach((id) => {
    const element = $(id);
    if (element) element.value = element.defaultValue;
  });
  renderGuardrailSizing();
  saveGuardrailPreferences();
}

function updateGuardrailPreferencesFromUi() {
  renderGuardrailPreferenceReadout();
  renderGuardrailSizing();
  saveGuardrailPreferences();
}

function selectGuardrailItem(ticker, mode) {
  const buckets = {
    research: state.guardrailsData?.research || [],
    fomo: state.guardrailsData?.fomo || [],
    sell_review: state.guardrailsData?.sell_review || [],
    sleep_on_it: state.guardrailsData?.sleep_on_it || [],
  };
  const item = (buckets[mode] || []).find((row) => row.ticker === ticker);
  if (!item) return;
  state.guardrailsSelected = item;
  renderGuardrailDetail(item);
}

function renderGuardrailDetail(item) {
  const box = $('guardrailDetail');
  if (!box) return;
  const noteKey = `marketSignalScanner.guardrailNote.${item.ticker}`;
  const note = localStorage.getItem(noteKey) || '';
  const prefs = guardrailPreferences();
  box.innerHTML = `
    <div class="guardrail-detail-head">
      <div>
        <h3>${escapeHtml(item.ticker)} Decision Checklist</h3>
        <p>${escapeHtml(item.name || '')}</p>
      </div>
      <span class="rec-pill ${recommendationClass(item.recommendation)}">${escapeHtml(item.recommendation || '')}</span>
    </div>
    ${tickerActionBar(item.ticker)}
    <div class="guardrail-detail-grid">
      <div>
        <h4>Posture</h4>
        <p>${escapeHtml(item.posture || '')}</p>
        <h4>Why It Flagged</h4>
        <ul>${(item.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join('')}</ul>
      </div>
      <div>
        <h4>Checklist Before Action</h4>
        <label class="guardrail-check"><input type="checkbox" /> I can explain the thesis without mentioning today's price move.</label>
        <label class="guardrail-check"><input type="checkbox" /> I checked valuation, debt, margin, and growth risks.</label>
        <label class="guardrail-check"><input type="checkbox" /> I know what would make me sell or stop buying.</label>
        <label class="guardrail-check"><input type="checkbox" /> I am using a starter size or DCA plan.</label>
        <div class="preference-note">Your current profile: risk ${prefs.riskTolerance}/10, horizon ${prefs.timeHorizon}/10, FOMO brake ${prefs.fomoBrake}/10.</div>
        <ul class="compact-list">${(item.checklist || []).map((line) => `<li>${escapeHtml(line)}</li>`).join('')}</ul>
      </div>
      <div class="guardrail-notes">
        <h4>Decision Journal</h4>
        <textarea id="guardrailNoteEditor" data-note-key="${escapeHtml(noteKey)}" placeholder="Write your thesis, reason to wait, or sell-review notes here.">${escapeHtml(note)}</textarea>
        <div class="hint">Saved locally in this browser.</div>
      </div>
    </div>
  `;
}

function wireOpportunityMap() {
  const canvas = $('opportunityCanvas');
  if (canvas && canvas.dataset.wired !== 'true') {
    canvas.dataset.wired = 'true';
    canvas.addEventListener('mousemove', (event) => {
      if (!state.opportunityPoints.length) return;
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      const hit = [...state.opportunityPoints].reverse().find((point) => Math.hypot(point.x - x, point.y - y) <= point.r + 4);
      state.opportunityHover = hit ? { row: hit.row, x, y } : null;
      drawOpportunityMap();
    });
    canvas.addEventListener('mouseleave', () => {
      state.opportunityHover = null;
      drawOpportunityMap();
    });
    canvas.addEventListener('click', () => {
      if (state.opportunityHover) selectOpportunityTicker(state.opportunityHover.row.ticker);
    });
  }
  ['opportunityAssetType', 'opportunityRecommendation', 'opportunityXAxis', 'opportunityYAxis', 'opportunityMinScore'].forEach((id) => {
    const element = $(id);
    if (element && element.dataset.wired !== 'true') {
      element.dataset.wired = 'true';
      element.addEventListener('change', () => {
        renderOpportunityBoards();
        drawOpportunityMap();
      });
    }
  });
}

function xMetricValue(row, key) {
  const value = Number(row[key]);
  return key === 'max_drawdown' ? Math.abs(value || 0) * 100 : key.includes('volatility') ? value * 100 : value;
}

function yMetricValue(row, key) {
  const value = Number(row[key]);
  return key.startsWith('return_') ? value * 100 : value;
}

function opportunityScale(values, start, size, padRatio = 0.08) {
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (!Number.isFinite(min) || !Number.isFinite(max)) { min = 0; max = 1; }
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * padRatio;
  min -= pad;
  max += pad;
  return {
    min,
    max,
    to: (value) => start + ((value - min) / (max - min || 1)) * size,
    from: (pixel) => min + ((pixel - start) / (size || 1)) * (max - min),
  };
}

function axisLabel(key) {
  const labels = {
    risk: 'Risk Composite',
    volatility_annual: 'Volatility %',
    max_drawdown: 'Drawdown %',
    rsi_14: 'RSI',
    score: 'Scanner Score',
    opportunity: 'Opportunity Quality',
    return_1m: '1M Return %',
    return_3m: '3M Return %',
    sharpe_like: 'Sharpe-like',
  };
  return labels[key] || key;
}

function recommendationColor(value) {
  return {
    'Strong Buy': '#16a34a',
    Buy: '#22c55e',
    Hold: '#64748b',
    Sell: '#f97316',
    'Strong Sell': '#dc2626',
  }[value] || '#64748b';
}

function recommendationClass(value) {
  return String(value || 'Hold').toLowerCase().replace(/\s+/g, '-');
}

function heatStyle(value, type) {
  const color = heatColor(value, type);
  return `background:${color.bg};color:${color.fg};`;
}

function heatColor(value, type) {
  if (!isFiniteNumber(value)) return { bg: '#f1f5f9', fg: '#64748b' };
  let score = Number(value);
  if (type === 'return') score = Math.max(-1, Math.min(1, score * 5));
  if (type === 'score') score = Math.max(-1, Math.min(1, score / 100));
  if (type === 'rsi') score = Math.max(-1, Math.min(1, (Math.abs(score - 50) / 50) * (score > 70 ? -1 : 1)));
  if (score >= 0) {
    const alpha = 0.16 + Math.abs(score) * 0.58;
    return { bg: `rgba(22, 163, 74, ${alpha})`, fg: '#052e16' };
  }
  const alpha = 0.16 + Math.abs(score) * 0.58;
  return { bg: `rgba(220, 38, 38, ${alpha})`, fg: '#450a0a' };
}

function heatValue(value, type) {
  if (type === 'return') return pctText(value);
  return numberText(value, type === 'score' ? 1 : 0);
}

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function pctText(value) {
  return isFiniteNumber(value) ? `${(value * 100).toFixed(1)}%` : 'n/a';
}

function numberText(value, digits = 1) {
  return isFiniteNumber(value) ? Number(value).toFixed(digits) : 'n/a';
}

function compactAxis(value) {
  return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1);
}

function helpTip(text) {
  return `<span class="help-tip" tabindex="0" data-help="${escapeHtml(text)}">?</span>`;
}

function chartParams() {
  normalizeChartControls();
  const params = new URLSearchParams({
    ticker: $('chartTicker').value.trim() || 'AAPL',
    period: $('chartPeriod').value,
    interval: $('chartInterval').value,
    chart_type: $('chartType').value,
    lookback: String(Number($('chartLookback').value || 260)),
    moving_averages: $('chartMa').value.trim() || '20,50,100,200',
    support_resistance: String($('showLevels').checked || $('showTrendlines').checked),
    bollinger: String($('showBollinger').checked),
    volume: String($('showVolume').checked),
    rsi: String($('showRsi').checked),
    macd: String($('showMacd').checked),
  });
  return params;
}

async function ensureInteractiveChart() {
  await loadChartTickerChoices();
  if (!state.chartData) await loadInteractiveChart();
  else drawInteractiveChart();
}

async function loadChartTickerChoices(force = false) {
  if (state.chartTickerChoices && !force) return;
  const select = $('chartTickerSelect');
  const list = $('chartTickerList');
  if (!select || !list) return;
  try {
    const data = await (await api('/api/chart/tickers')).json();
    const tickers = data.tickers || [];
    state.chartTickerChoices = tickers;
    const current = $('chartTicker').value.trim().toUpperCase();
    select.innerHTML = [
      '<option value="">Pick from list...</option>',
      ...tickers.map((ticker) => `<option value="${escapeHtml(ticker)}">${escapeHtml(ticker)}</option>`),
    ].join('');
    list.innerHTML = tickers.map((ticker) => `<option value="${escapeHtml(ticker)}"></option>`).join('');
    syncChartTickerSelect(current);
  } catch (error) {
    state.chartTickerChoices = [];
    list.innerHTML = '';
    select.innerHTML = '<option value="">Type custom ticker...</option>';
  }
}

function syncChartTickerSelect(ticker) {
  const select = $('chartTickerSelect');
  if (!select) return;
  const normalized = String(ticker || '').trim().toUpperCase();
  const hasOption = [...select.options].some((option) => option.value === normalized);
  select.value = hasOption ? normalized : '';
}

async function loadInteractiveChart() {
  const status = $('interactiveChartStatus');
  normalizeChartControls();
  status.textContent = 'Loading chart...';
  $('loadInteractiveChart').disabled = true;
  try {
    const data = await (await api(`/api/chart/interactive?${chartParams().toString()}`)).json();
    state.chartData = data;
    state.chartView = { start: 0, end: Math.max(0, data.rows.length - 1) };
    state.chartHover = null;
    state.chartSelection = null;
    renderChartLegend();
    drawInteractiveChart();
    const summary = data.summary || {};
    const close = money(summary.last_close);
    const change = signed(summary.change);
    const changePct = signed(summary.change_pct, '%');
    status.textContent = `${data.ticker}: ${close} (${change}, ${changePct}) across ${summary.bars || data.rows.length} bars.`;
  } catch (error) {
    status.textContent = `Could not load chart: ${error.message}`;
  } finally {
    $('loadInteractiveChart').disabled = false;
  }
}

function resetInteractiveChart() {
  if (!state.chartData) return;
  state.chartView = { start: 0, end: Math.max(0, state.chartData.rows.length - 1) };
  state.chartHover = null;
  state.chartSelection = null;
  drawInteractiveChart();
}

function drawInteractiveChart() {
  const canvas = $('interactiveChartCanvas');
  const tooltip = $('interactiveChartTooltip');
  if (!canvas || !state.chartData) return;
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(720, Math.floor(rect.width || canvas.clientWidth || 1200));
  const height = Math.max(520, Math.floor(rect.height || canvas.clientHeight || 720));
  if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
    canvas.width = Math.floor(width * dpr);
    canvas.height = Math.floor(height * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = cssVar('--panel', '#ffffff');
  ctx.fillRect(0, 0, width, height);

  const rows = visibleChartRows();
  if (!rows.length) return;
  renderChartLegend();
  const panels = chartPanels(height);
  drawPricePanel(ctx, rows, panels.price, width);
  if (panels.volume) drawVolumePanel(ctx, rows, panels.volume, width);
  if (panels.rsi) drawLinePanel(ctx, rows, panels.rsi, width, ['rsi_14'], ['#7c3aed'], { min: 0, max: 100, guides: [30, 70], label: 'RSI' });
  if (panels.macd) drawLinePanel(ctx, rows, panels.macd, width, ['macd', 'macd_signal'], ['#2563eb', '#f97316'], { histogram: 'macd_hist', label: 'MACD' });
  if (state.chartHover) drawCrosshair(ctx, rows, panels, width, tooltip);
  else if (tooltip) tooltip.style.display = 'none';
  if (state.chartSelection) drawChartSelection(ctx);
}

function normalizeChartControls() {
  const period = $('chartPeriod')?.value || '1y';
  const interval = $('chartInterval')?.value || '1d';
  const allowed = allowedChartIntervals(period);
  if (!allowed.includes(interval)) {
    $('chartInterval').value = defaultChartInterval(period);
  }
}

function applyPeriodDefaults() {
  const period = $('chartPeriod').value;
  normalizeChartControls();
  const defaults = {
    '5d': 390,
    '1mo': 500,
    '3mo': 500,
    '6mo': 520,
    '1y': 260,
    '2y': 520,
    '5y': 1260,
    max: 5000,
  };
  $('chartLookback').value = String(defaults[period] || 260);
}

function allowedChartIntervals(period) {
  if (['5d', '1mo'].includes(period)) return ['5m', '15m', '30m', '1h', '1d'];
  if (['3mo', '6mo', '1y', '2y'].includes(period)) return ['1h', '1d', '1wk', '1mo'];
  return ['1d', '1wk', '1mo'];
}

function defaultChartInterval(period) {
  if (['5d', '1mo'].includes(period)) return '15m';
  return '1d';
}

function chartPanels(height) {
  const top = 18;
  const bottom = 22;
  const gap = 10;
  const volume = $('showVolume').checked;
  const rsi = $('showRsi').checked;
  const macd = $('showMacd').checked;
  const smallCount = Number(volume) + Number(rsi) + Number(macd);
  const smallHeight = smallCount ? Math.max(82, Math.min(120, Math.floor((height - top - bottom) * 0.15))) : 0;
  const priceHeight = height - top - bottom - smallCount * smallHeight - smallCount * gap;
  let y = top;
  const panels = { price: { x: 58, y, w: 0, h: priceHeight } };
  y += priceHeight + gap;
  if (volume) { panels.volume = { x: 58, y, w: 0, h: smallHeight }; y += smallHeight + gap; }
  if (rsi) { panels.rsi = { x: 58, y, w: 0, h: smallHeight }; y += smallHeight + gap; }
  if (macd) panels.macd = { x: 58, y, w: 0, h: smallHeight };
  return panels;
}

function visibleChartRows() {
  const rows = state.chartData?.rows || [];
  if (!rows.length) return [];
  const start = Math.max(0, Math.min(state.chartView.start, rows.length - 1));
  const end = Math.max(start, Math.min(state.chartView.end, rows.length - 1));
  return rows.slice(start, end + 1);
}

function drawPricePanel(ctx, rows, panel, width) {
  const inner = chartInner(panel, width);
  const overlayKeys = activePriceOverlayKeys(rows);
  const values = rows.flatMap((row) => [row.high, row.low, ...overlayKeys.map((key) => row[key])]).filter(isNum);
  const scale = valueScale(values, inner.y, inner.h, 0.06);
  drawPanelFrame(ctx, inner, scale, 'Price');
  if ($('showBollinger').checked) {
    drawSeries(ctx, rows, inner, scale, 'bb_upper', CHART_COLORS.bollinger, 1, [4, 4]);
    drawSeries(ctx, rows, inner, scale, 'bb_mid', CHART_COLORS.bollinger, 0.8, [2, 4]);
    drawSeries(ctx, rows, inner, scale, 'bb_lower', CHART_COLORS.bollinger, 1, [4, 4]);
  }
  if ($('showSma').checked) {
    smaKeys(rows).forEach((key, index) => {
      drawSeries(ctx, rows, inner, scale, key, CHART_COLORS.sma[index % CHART_COLORS.sma.length], 1.35);
    });
  }
  if ($('showEma').checked) {
    drawSeries(ctx, rows, inner, scale, 'ema_20', CHART_COLORS.ema20, 1.15);
    drawSeries(ctx, rows, inner, scale, 'ema_50', CHART_COLORS.ema50, 1.15);
  }
  if (state.chartData.chart_type === 'line') drawSeries(ctx, rows, inner, scale, 'close', cssVar('--accent', '#0f766e'), 2);
  else drawCandles(ctx, rows, inner, scale);
  if ($('showLevels').checked) drawLevels(ctx, inner, scale);
  if ($('showTrendlines').checked) drawTrendlines(ctx, rows, inner, scale);
  drawChartTitle(ctx, inner);
  drawTimeAxis(ctx, rows, inner);
}

function activePriceOverlayKeys(rows) {
  const keys = [];
  if ($('showSma').checked) keys.push(...smaKeys(rows));
  if ($('showEma').checked) keys.push('ema_20', 'ema_50');
  if ($('showBollinger').checked) keys.push('bb_upper', 'bb_mid', 'bb_lower');
  return keys;
}

function smaKeys(rows) {
  return Object.keys(rows[0] || {}).filter((key) => key.startsWith('sma_')).sort((a, b) => Number(a.slice(4)) - Number(b.slice(4)));
}

function renderChartLegend() {
  const box = $('chartLegend');
  if (!box || !state.chartData) return;
  const rows = state.chartData.rows || [];
  const items = [];
  if ($('showSma').checked) {
    smaKeys(rows).forEach((key, index) => {
      items.push({ label: key.replace('sma_', 'SMA '), color: CHART_COLORS.sma[index % CHART_COLORS.sma.length] });
    });
  }
  if ($('showEma').checked) {
    items.push({ label: 'EMA 20', color: CHART_COLORS.ema20 });
    items.push({ label: 'EMA 50', color: CHART_COLORS.ema50 });
  }
  if ($('showBollinger').checked) items.push({ label: 'Bollinger 20,2', color: CHART_COLORS.bollinger, dashed: true });
  if ($('showLevels').checked) {
    items.push({ label: 'Support', color: CHART_COLORS.support, dashed: true });
    items.push({ label: 'Resistance', color: CHART_COLORS.resistance, dashed: true });
  }
  if ($('showTrendlines').checked) items.push({ label: 'Trendlines', color: CHART_COLORS.trendSupport, dashed: true });
  box.innerHTML = items.length
    ? items.map((item) => `<span class="chart-legend-item"><span class="chart-swatch${item.dashed ? ' dashed' : ''}" style="--swatch:${escapeHtml(item.color)}"></span>${escapeHtml(item.label)}</span>`).join('')
    : '<span class="muted">No price overlays enabled.</span>';
}

function drawCandles(ctx, rows, inner, scale) {
  const step = inner.w / Math.max(1, rows.length - 1);
  const bodyWidth = Math.max(2, Math.min(12, step * 0.62));
  rows.forEach((row, index) => {
    const x = xForIndex(index, rows.length, inner);
    const up = row.close >= row.open;
    ctx.strokeStyle = up ? '#16a34a' : '#dc2626';
    ctx.fillStyle = ctx.strokeStyle;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, scale.y(row.high));
    ctx.lineTo(x, scale.y(row.low));
    ctx.stroke();
    const yOpen = scale.y(row.open);
    const yClose = scale.y(row.close);
    const top = Math.min(yOpen, yClose);
    const bodyHeight = Math.max(1, Math.abs(yClose - yOpen));
    ctx.fillRect(x - bodyWidth / 2, top, bodyWidth, bodyHeight);
  });
}

function drawSeries(ctx, rows, inner, scale, key, color, lineWidth = 1.4, dash = []) {
  ctx.save();
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.setLineDash(dash);
  ctx.beginPath();
  let started = false;
  rows.forEach((row, index) => {
    if (!isNum(row[key])) {
      started = false;
      return;
    }
    const x = xForIndex(index, rows.length, inner);
    const y = scale.y(row[key]);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
  ctx.restore();
}

function drawVolumePanel(ctx, rows, panel, width) {
  const inner = chartInner(panel, width);
  const maxVol = Math.max(...rows.map((row) => Number(row.volume || 0)), 1);
  drawPanelFrame(ctx, inner, { min: 0, max: maxVol, y: (v) => inner.y + inner.h - (v / maxVol) * inner.h }, 'Volume');
  const step = inner.w / Math.max(1, rows.length - 1);
  const barWidth = Math.max(2, Math.min(10, step * 0.68));
  rows.forEach((row, index) => {
    const x = xForIndex(index, rows.length, inner);
    const h = Math.max(1, (Number(row.volume || 0) / maxVol) * inner.h);
    ctx.fillStyle = row.close >= row.open ? 'rgba(22, 163, 74, 0.42)' : 'rgba(220, 38, 38, 0.42)';
    ctx.fillRect(x - barWidth / 2, inner.y + inner.h - h, barWidth, h);
  });
}

function drawLinePanel(ctx, rows, panel, width, keys, colors, options = {}) {
  const inner = chartInner(panel, width);
  const values = rows.flatMap((row) => keys.map((key) => row[key]).concat(options.histogram ? [row[options.histogram]] : [])).filter(isNum);
  const scale = options.min !== undefined ? { min: options.min, max: options.max, y: (v) => inner.y + inner.h - ((v - options.min) / (options.max - options.min || 1)) * inner.h } : valueScale(values, inner.y, inner.h, 0.18);
  drawPanelFrame(ctx, inner, scale, options.label || '');
  (options.guides || []).forEach((guide) => drawGuide(ctx, inner, scale.y(guide), String(guide)));
  if (options.histogram) drawHistogram(ctx, rows, inner, scale, options.histogram);
  keys.forEach((key, index) => drawSeries(ctx, rows, inner, scale, key, colors[index], 1.35));
}

function drawHistogram(ctx, rows, inner, scale, key) {
  const zero = scale.y(0);
  const step = inner.w / Math.max(1, rows.length - 1);
  const barWidth = Math.max(2, Math.min(10, step * 0.68));
  rows.forEach((row, index) => {
    if (!isNum(row[key])) return;
    const x = xForIndex(index, rows.length, inner);
    const y = scale.y(row[key]);
    ctx.fillStyle = row[key] >= 0 ? 'rgba(22, 163, 74, 0.32)' : 'rgba(220, 38, 38, 0.32)';
    ctx.fillRect(x - barWidth / 2, Math.min(y, zero), barWidth, Math.max(1, Math.abs(zero - y)));
  });
}

function drawPanelFrame(ctx, inner, scale, label) {
  ctx.strokeStyle = cssVar('--line', '#d9e0e7');
  ctx.lineWidth = 1;
  ctx.strokeRect(inner.x, inner.y, inner.w, inner.h);
  ctx.fillStyle = cssVar('--muted', '#667085');
  ctx.font = '11px ui-sans-serif, system-ui';
  if (label) ctx.fillText(label, inner.x + 8, inner.y + 14);
  for (let i = 0; i <= 4; i += 1) {
    const y = inner.y + (inner.h / 4) * i;
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.22)';
    ctx.beginPath();
    ctx.moveTo(inner.x, y);
    ctx.lineTo(inner.x + inner.w, y);
    ctx.stroke();
    const value = scale.max - ((scale.max - scale.min) / 4) * i;
    ctx.fillStyle = cssVar('--muted', '#667085');
    ctx.fillText(compactNumber(value), inner.x + inner.w + 8, y + 4);
  }
}

function drawGuide(ctx, inner, y, label) {
  ctx.save();
  ctx.strokeStyle = 'rgba(100, 116, 139, 0.42)';
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(inner.x, y);
  ctx.lineTo(inner.x + inner.w, y);
  ctx.stroke();
  ctx.fillStyle = cssVar('--muted', '#667085');
  ctx.fillText(label, inner.x + inner.w + 8, y + 4);
  ctx.restore();
}

function drawLevels(ctx, inner, scale) {
  (state.chartData.levels || []).forEach((level) => {
    if (!isNum(level.price)) return;
    const y = scale.y(level.price);
    ctx.save();
    ctx.strokeStyle = level.type === 'support' ? CHART_COLORS.support : CHART_COLORS.resistance;
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.moveTo(inner.x, y);
    ctx.lineTo(inner.x + inner.w, y);
    ctx.stroke();
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillText(`${level.type} ${money(level.price)}`, inner.x + 8, y - 4);
    ctx.restore();
  });
}

function drawTrendlines(ctx, rows, inner, scale) {
  (state.chartData.trendlines || []).forEach((line) => {
    const points = (line.points || []).map((point) => ({ x: rowIndexForDate(rows, point.date), y: point.value })).filter((point) => point.x >= 0 && isNum(point.y));
    if (points.length < 2) return;
    ctx.strokeStyle = line.type === 'trend_support' ? CHART_COLORS.trendSupport : CHART_COLORS.trendResistance;
    ctx.lineWidth = 1.2;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xForIndex(point.x, rows.length, inner);
      const y = scale.y(point.y);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
  });
}

function drawChartTitle(ctx, inner) {
  const summary = state.chartData.summary || {};
  ctx.fillStyle = cssVar('--ink', '#17212b');
  ctx.font = '700 15px ui-sans-serif, system-ui';
  ctx.fillText(`${state.chartData.ticker} ${money(summary.last_close)} ${signed(summary.change_pct, '%')}`, inner.x, 15);
}

function drawTimeAxis(ctx, rows, inner) {
  ctx.fillStyle = cssVar('--muted', '#667085');
  ctx.font = '11px ui-sans-serif, system-ui';
  const ticks = Math.min(6, rows.length);
  for (let i = 0; i < ticks; i += 1) {
    const index = Math.round((rows.length - 1) * (i / Math.max(1, ticks - 1)));
    const x = xForIndex(index, rows.length, inner);
    ctx.fillText(shortDate(rows[index].date), x - 28, inner.y + inner.h + 17);
  }
}

function drawCrosshair(ctx, rows, panels, width, tooltip) {
  const inner = chartInner(panels.price, width);
  const index = Math.max(0, Math.min(rows.length - 1, Math.round(((state.chartHover.x - inner.x) / inner.w) * (rows.length - 1))));
  const row = rows[index];
  const x = xForIndex(index, rows.length, inner);
  ctx.save();
  ctx.strokeStyle = 'rgba(15, 23, 42, 0.42)';
  ctx.setLineDash([3, 3]);
  Object.values(panels).filter(Boolean).forEach((panel) => {
    const area = chartInner(panel, width);
    ctx.beginPath();
    ctx.moveTo(x, area.y);
    ctx.lineTo(x, area.y + area.h);
    ctx.stroke();
  });
  ctx.restore();
  if (tooltip && row) {
    tooltip.style.display = 'block';
    tooltip.style.left = `${Math.min(state.chartHover.x + 16, inner.x + inner.w - 220)}px`;
    tooltip.style.top = `${Math.max(12, state.chartHover.y + 16)}px`;
    tooltip.innerHTML = `
      <strong>${escapeHtml(shortDate(row.date))}</strong>
      <span>O ${money(row.open)} H ${money(row.high)}</span>
      <span>L ${money(row.low)} C ${money(row.close)}</span>
      <span>Vol ${compactNumber(row.volume || 0)}</span>
      ${isNum(row.rsi_14) ? `<span>RSI ${row.rsi_14.toFixed(1)}</span>` : ''}
    `;
  }
}

function drawChartSelection(ctx) {
  const selection = state.chartSelection;
  if (!selection) return;
  const x = Math.min(selection.startX, selection.currentX);
  const y = Math.min(selection.startY, selection.currentY);
  const w = Math.abs(selection.currentX - selection.startX);
  const h = Math.abs(selection.currentY - selection.startY);
  ctx.save();
  ctx.fillStyle = 'rgba(37, 99, 235, 0.12)';
  ctx.strokeStyle = 'rgba(37, 99, 235, 0.72)';
  ctx.lineWidth = 1;
  ctx.setLineDash([5, 4]);
  ctx.fillRect(x, y, w, h);
  ctx.strokeRect(x, y, w, h);
  ctx.restore();
}

function chartInner(panel, width) {
  return { x: panel.x, y: panel.y, w: Math.max(120, width - panel.x - 84), h: panel.h };
}

function valueScale(values, y, h, padRatio = 0.08) {
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (!Number.isFinite(min) || !Number.isFinite(max)) { min = 0; max = 1; }
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * padRatio;
  min -= pad;
  max += pad;
  return { min, max, y: (value) => y + h - ((value - min) / (max - min || 1)) * h };
}

function xForIndex(index, length, inner) {
  return inner.x + (length <= 1 ? 0 : (index / (length - 1)) * inner.w);
}

function rowIndexForDate(rows, date) {
  return rows.findIndex((row) => row.date === date);
}

function isNum(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function money(value) {
  return isNum(value) ? value.toLocaleString(undefined, { maximumFractionDigits: value >= 100 ? 2 : 4 }) : 'n/a';
}

function signed(value, suffix = '') {
  if (!isNum(value)) return 'n/a';
  const sign = value > 0 ? '+' : '';
  return `${sign}${value.toFixed(2)}${suffix}`;
}

function compactNumber(value) {
  if (!isNum(Number(value))) return 'n/a';
  return Number(value).toLocaleString(undefined, { notation: 'compact', maximumFractionDigits: 2 });
}

function shortDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: '2-digit' });
}

function wireInteractiveChart() {
  const canvas = $('interactiveChartCanvas');
  if (!canvas || canvas.dataset.wired === 'true') return;
  canvas.dataset.wired = 'true';
  updateChartMouseMode();
  canvas.addEventListener('wheel', (event) => {
    if (!state.chartData) return;
    event.preventDefault();
    const rows = state.chartData.rows || [];
    const total = rows.length;
    if (total < 2) return;
    const rect = canvas.getBoundingClientRect();
    const xRatio = Math.max(0, Math.min(1, (event.clientX - rect.left - 58) / Math.max(1, rect.width - 142)));
    const view = state.chartView;
    const span = Math.max(12, view.end - view.start + 1);
    const nextSpan = Math.max(12, Math.min(total, Math.round(span * (event.deltaY > 0 ? 1.18 : 0.84))));
    const center = view.start + span * xRatio;
    let start = Math.round(center - nextSpan * xRatio);
    let end = start + nextSpan - 1;
    if (start < 0) { end -= start; start = 0; }
    if (end >= total) { start -= end - total + 1; end = total - 1; }
    state.chartView = { start: Math.max(0, start), end: Math.min(total - 1, end) };
    drawInteractiveChart();
  }, { passive: false });
  canvas.addEventListener('mousedown', (event) => {
    if (!state.chartData) return;
    const rect = canvas.getBoundingClientRect();
    if ($('chartMouseMode').value === 'box') {
      state.chartSelection = {
        startX: event.clientX - rect.left,
        startY: event.clientY - rect.top,
        currentX: event.clientX - rect.left,
        currentY: event.clientY - rect.top,
      };
      state.chartDrag = null;
      drawInteractiveChart();
      return;
    }
    state.chartDrag = { x: event.clientX, start: state.chartView.start, end: state.chartView.end };
  });
  window.addEventListener('mouseup', () => {
    if (state.chartSelection) applyChartBoxZoom();
    state.chartDrag = null;
    state.chartSelection = null;
    drawInteractiveChart();
  });
  canvas.addEventListener('mousemove', (event) => {
    const rect = canvas.getBoundingClientRect();
    if (state.chartSelection) {
      state.chartSelection.currentX = event.clientX - rect.left;
      state.chartSelection.currentY = event.clientY - rect.top;
    } else if (state.chartDrag && state.chartData) {
      const rows = state.chartData.rows || [];
      const span = state.chartDrag.end - state.chartDrag.start + 1;
      const barWidth = Math.max(1, (rect.width - 142) / Math.max(1, span));
      const shift = Math.round((state.chartDrag.x - event.clientX) / barWidth);
      let start = state.chartDrag.start + shift;
      let end = state.chartDrag.end + shift;
      if (start < 0) { end -= start; start = 0; }
      if (end >= rows.length) { start -= end - rows.length + 1; end = rows.length - 1; }
      state.chartView = { start: Math.max(0, start), end: Math.max(0, end) };
    }
    state.chartHover = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    drawInteractiveChart();
  });
  canvas.addEventListener('mouseleave', () => {
    state.chartHover = null;
    state.chartDrag = null;
    if (!state.chartSelection) drawInteractiveChart();
  });
  window.addEventListener('resize', () => {
    if (state.currentTab === 'chart') drawInteractiveChart();
  });
}

function updateChartMouseMode() {
  const canvas = $('interactiveChartCanvas');
  if (!canvas) return;
  const mode = $('chartMouseMode').value;
  canvas.classList.toggle('pan-mode', mode === 'pan');
  canvas.classList.toggle('box-mode', mode === 'box');
  state.chartDrag = null;
  state.chartSelection = null;
}

function applyChartBoxZoom() {
  if (!state.chartData || !state.chartSelection) return;
  const canvas = $('interactiveChartCanvas');
  const rect = canvas.getBoundingClientRect();
  const rows = visibleChartRows();
  if (rows.length < 2) return;
  const inner = { x: 58, w: Math.max(120, rect.width - 58 - 84) };
  const left = Math.max(inner.x, Math.min(state.chartSelection.startX, state.chartSelection.currentX));
  const right = Math.min(inner.x + inner.w, Math.max(state.chartSelection.startX, state.chartSelection.currentX));
  if (right - left < 24) return;
  const startOffset = Math.floor(((left - inner.x) / inner.w) * (rows.length - 1));
  const endOffset = Math.ceil(((right - inner.x) / inner.w) * (rows.length - 1));
  const currentStart = state.chartView.start;
  const start = Math.max(0, currentStart + startOffset);
  const end = Math.min((state.chartData.rows || []).length - 1, currentStart + Math.max(startOffset + 1, endOffset));
  if (end > start) state.chartView = { start, end };
}

async function createJob(payload) {
  const response = await api('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const job = await response.json();
  state.currentJobId = job.id;
  renderLatestActivity(job);
  renderRunProgress(job);
  await loadActivity();
  startPolling(job.id);
  return job;
}

async function createJobWithStatus(payload, statusId, startingMessage) {
  const status = $(statusId);
  const button = payload.command === 'news' ? $('runNews') : null;
  if (status) status.textContent = startingMessage;
  if (button) {
    button.disabled = true;
    button.textContent = 'Starting...';
  }
  try {
    const job = await createJob(payload);
    if (status) status.textContent = `${job.command} job started. Status: ${job.status}.`;
    if (button) button.textContent = 'Summary Running...';
    return job;
  } catch (error) {
    if (status) status.textContent = `Could not start ${payload.command}: ${error.message}`;
    if (button) {
      button.disabled = false;
      button.textContent = 'Run News Summary';
    }
    return null;
  }
}

function runNewsFromUi() {
  const tickerInput = $('newsTicker');
  const status = $('newsStatus');
  if (!tickerInput) {
    if (status) status.textContent = 'News summary ticker input was not found. Refresh the page.';
    return;
  }
  const ticker = tickerInput.value.trim();
  if (!ticker) {
    if (status) status.textContent = 'Enter a ticker before running News Summary.';
    return;
  }
  createJobWithStatus({
    command: 'news',
    ticker,
  }, 'newsStatus', 'Starting news summary...');
}

function wireNewsAction() {
  const button = $('runNews');
  if (!button || button.dataset.wired === 'true') return;
  button.dataset.wired = 'true';
  button.addEventListener('click', (event) => {
    event.preventDefault();
    runNewsFromUi();
  });
}

function startPolling(jobId) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    try {
      const job = await (await api(`/api/jobs/${jobId}`)).json();
      renderLatestActivity(job);
      renderRunProgress(job);
      renderActivityPageStatus(job);
      if (isTerminalStatus(job.status)) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        await loadRuns();
        await loadActivity();
        if (job.command === 'chart' && job.status === 'completed') showChartFromJob(job);
        if (job.command === 'news' && job.status === 'completed') showNewsFromJob(job);
        if (['scan', 'backtest'].includes(job.command) && job.status === 'completed') showRunReportFromJob(job);
      }
    } catch (error) {
      renderLatestActivity({ status: 'failed', command: 'unknown', error: error.message });
      renderRunProgress({ status: 'failed', command: 'unknown', error: error.message });
      clearInterval(state.pollTimer);
    }
  }, 1500);
}

function renderLatestActivity(job) {
  if (!$('latestActivity')) return;
  const output = job.output_dir ? `<div>Output: <code>${job.run_kind}/${job.output_dir}</code></div>` : '';
  const error = job.error ? `<div class="danger">${escapeHtml(job.error)}</div>` : '';
  $('latestActivity').className = 'job-card';
  $('latestActivity').innerHTML = `
    <div><span class="badge ${job.status}">${job.status}</span> <strong>${escapeHtml(job.title || job.command)}</strong></div>
    <div class="hint">${job.started_at || job.created_at || ''}${job.finished_at ? ` → ${job.finished_at}` : ''}</div>
    ${output}${error}
    ${job.logs ? logDetails(job.logs, 5000) : ''}
  `;
}

function renderRunProgress(job) {
  if (!$('runProgress') || !['scan', 'backtest'].includes(job.command)) return;
  const output = job.output_dir ? `<div>Output: <code>${job.run_kind}/${job.output_dir}</code></div>` : '';
  const error = job.error ? `<div class="danger">${escapeHtml(job.error)}</div>` : '';
  $('runProgress').className = 'job-card';
  $('runProgress').innerHTML = `
    <div><span class="badge ${job.status}">${job.status}</span> <strong>${escapeHtml(job.command)}</strong></div>
    <div class="hint">${job.started_at || job.created_at || ''}${job.finished_at ? ` → ${job.finished_at}` : ''}</div>
    ${output}${error}
    ${job.logs ? logDetails(job.logs, 5000) : ''}
  `;
  if (job.status !== 'completed' && $('runReportPreview')) {
    $('runReportPreview').className = 'panel preview empty';
    $('runReportPreview').textContent = `${job.command === 'scan' ? 'Scan' : 'Backtest'} report will appear here when the run completes.`;
  }
}

async function loadRunProgress() {
  if (!$('runProgress')) return;
  const jobs = await (await api('/api/jobs')).json();
  const job = jobs.find((item) => ['scan', 'backtest'].includes(item.command));
  if (!job) {
    $('runProgress').className = 'job-card muted';
    $('runProgress').textContent = 'No scan or backtest started yet.';
    return;
  }
  renderRunProgress(job);
  if (job.status === 'completed') showRunReportFromJob(job);
}

function renderActivityPageStatus(job) {
  if (job.command !== 'news') return;
  const status = $('newsStatus');
  if (!status) return;
  const output = job.output_dir ? ` Output: ${job.run_kind}/${job.output_dir}.` : '';
  const error = job.error ? ` Error: ${job.error}.` : '';
  status.textContent = `News summary job ${job.status}.${output}${error}`;
  if (isTerminalStatus(job.status)) {
    const button = $('runNews');
    if (button) {
      button.disabled = false;
      button.textContent = 'Run News Summary';
    }
  }
}

function latestActiveItem(items, type) {
  return items.find((item) => item.activity_type === type && !isTerminalStatus(item.status));
}

function latestItem(items, type) {
  return items.find((item) => item.activity_type === type);
}

async function loadActivity() {
  const items = await (await api('/api/activity')).json();
  const box = $('activityList');
  if (items.length) renderLatestActivity(items[0]);
  if (!items.length) {
    box.innerHTML = '<div class="panel muted">No activity yet.</div>';
    return;
  }
  box.innerHTML = items.map((job) => `
    <section class="panel">
      <div class="activity-head">
        <div><span class="badge ${job.status}">${job.status}</span> <strong>${escapeHtml(job.title || job.command)}</strong> <code>${job.id.slice(0, 8)}</code></div>
        ${job.cancellable ? `<button class="danger-button small" data-cancel-url="${job.cancel_url}">Cancel</button>` : ''}
      </div>
      ${job.subtitle ? `<p class="hint">${escapeHtml(job.subtitle)}</p>` : ''}
      <p>${job.started_at || job.created_at || ''}${job.finished_at ? ` → ${job.finished_at}` : ''}</p>
      ${job.output_dir ? `<p>Output: <code>${job.run_kind}/${job.output_dir}</code></p>` : ''}
      ${job.error ? `<p class="danger">${escapeHtml(job.error)}</p>` : ''}
      ${job.logs ? logDetails(job.logs, 8000) : ''}
    </section>`).join('');
  box.querySelectorAll('[data-cancel-url]').forEach((button) => {
    button.addEventListener('click', () => cancelActivity(button.dataset.cancelUrl));
  });
}

function logDetails(logs, maxLength = 5000) {
  return `
    <details class="log-details">
      <summary>Show technical log</summary>
      <pre class="job-log">${escapeHtml(String(logs || '').slice(-maxLength))}</pre>
    </details>
  `;
}

async function fetchLatestSessionId(type, savedId) {
  const items = await (await api('/api/activity')).json();
  const active = latestActiveItem(items, type);
  if (active) return active.id;
  if (savedId) return savedId;
  const latest = latestItem(items, type);
  return latest ? latest.id : null;
}

function startActivityPolling() {
  if (state.activityPollTimer) clearInterval(state.activityPollTimer);
  state.activityPollTimer = setInterval(async () => {
    try {
      await loadActivity();
    } catch (_) {
      // Keep the UI quiet during transient server restarts.
    }
  }, 5000);
}

function isTerminalStatus(status) {
  return ['completed', 'failed', 'cancelled'].includes(status);
}

function formatEventsAsLog(events) {
  return events.map((event) => `${event.created_at || ''} [${event.kind || 'event'}] ${event.message || ''}`).join('\n');
}

async function cancelActivity(cancelUrl) {
  if (!cancelUrl) return;
  try {
    const item = await (await api(cancelUrl, { method: 'POST' })).json();
    renderLatestActivity(item);
    await loadActivity();
    if (item.command === 'news') renderActivityPageStatus(item);
  } catch (error) {
    renderLatestActivity({ status: 'failed', command: 'cancel', error: `Cancel failed: ${error.message}` });
  }
}

async function loadRuns() {
  const runs = await (await api('/api/runs')).json();
  state.runs = runs;
  renderRunList('scanRuns', 'scans', runs.scans);
  renderRunList('backtestRuns', 'backtests', runs.backtests);
  renderRunList('chartRuns', 'charts', runs.charts);
  renderRunList('newsRuns', 'news', runs.news || []);
  renderRunList('agentRuns', 'agents', runs.agents || []);
  renderRunList('trendCatcherRuns', 'trend-catcher', runs['trend-catcher'] || []);
}

function renderRunList(containerId, kind, runs) {
  const container = $(containerId);
  if (!runs.length) {
    container.innerHTML = '<div class="muted">No runs yet.</div>';
    return;
  }
  container.innerHTML = runs.slice(0, 20).map((run) => `
    <div class="run-item">
      <div class="run-title-row">
        <h4>${escapeHtml(formatRunTitle(run.id))}</h4>
        <button class="trash-button" data-delete-kind="${kind}" data-delete-run="${run.id}" title="Delete this run">&#128465;</button>
      </div>
      <div class="run-meta">${escapeHtml(formatRunSubtitle(run.id))}</div>
      <div class="file-row">
        ${run.files.map((file) => `<button class="file-pill" data-kind="${kind}" data-run="${run.id}" data-file="${file}">${file}</button>`).join('')}
      </div>
    </div>
  `).join('');
  container.querySelectorAll('.file-pill').forEach((button) => {
    button.addEventListener('click', () => previewFile(button.dataset.kind, button.dataset.run, button.dataset.file));
  });
  container.querySelectorAll('[data-delete-run]').forEach((button) => {
    button.addEventListener('click', () => deleteRun(button.dataset.deleteKind, button.dataset.deleteRun));
  });
}

async function deleteRun(kind, runId) {
  if (!kind || !runId) return;
  if (!confirm(`Delete ${kind}/${runId}? This cannot be undone.`)) return;
  await api(`/api/runs/${kind}/${encodeURIComponent(runId)}`, { method: 'DELETE' });
  clearPreviewIfShowing(kind, runId);
  await loadRuns();
}

async function deleteRunsForKind(kind) {
  if (!kind) return;
  if (!confirm(`Delete all ${kind} runs? This cannot be undone.`)) return;
  await api(`/api/runs/${kind}`, { method: 'DELETE' });
  clearPreviewIfShowing(kind, '');
  await loadRuns();
}

function clearPreviewIfShowing(kind, runId) {
  const link = $('downloadLink');
  const href = link ? link.getAttribute('href') || '' : '';
  if (!href.includes(`/api/files/${kind}/`)) return;
  if (runId && !href.includes(`/api/files/${kind}/${runId}/`)) return;
  $('fileViewer').className = 'viewer empty';
  $('fileViewer').textContent = 'Select an output file.';
  link.href = '#';
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
    if (kind === 'charts' && filename.toLowerCase() === 'chart_report.md') {
      const detail = await (await api(`/api/runs/charts/${runId}`)).json();
      const image = detail.files.find((file) => file.name.endsWith('_technical_chart.png'));
      $('fileViewer').innerHTML = `
        <div class="chart-report-preview">
          ${image ? `<img class="chart-report-image" src="${image.url}" alt="${escapeHtml(runId)} technical chart" />` : ''}
          <div class="markdown-body">${renderMarkdown(preview.text)}</div>
        </div>
      `;
    } else {
      $('fileViewer').innerHTML = filename.toLowerCase().endsWith('.md')
        ? `<div class="markdown-body">${renderMarkdown(preview.text)}</div>`
        : `<pre>${escapeHtml(preview.text)}</pre>`;
    }
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
  let reportHtml = '<div class="empty">No chart report markdown found.</div>';
  if (report) {
    const preview = await (await api(`/api/preview/charts/${job.output_dir}/${report.name}`)).json();
    reportHtml = `<div class="markdown-body">${renderMarkdown(preview.text || '')}</div>`;
  }
  $('chartPreview').className = 'panel preview chart-report-preview';
  $('chartPreview').innerHTML = `
    <div class="panel-title-row">
      <div>
        <h3>Static Chart Report</h3>
        <p class="compact">Saved technical-analysis snapshot. The image is for visual inspection; the explanation below tells you how to use it.</p>
      </div>
      <div class="button-row">
        ${image ? `<a class="download" href="${image.url}" target="_blank">Open PNG</a>` : ''}
        ${report ? `<a class="download" href="${report.url}" target="_blank">Open Markdown</a>` : ''}
      </div>
    </div>
    ${image ? `<img class="chart-report-image" src="${image.url}" alt="${escapeHtml(job.output_dir)} technical chart" />` : '<div class="empty">No chart image found.</div>'}
    ${reportHtml}
  `;
}

async function showNewsFromJob(job) {
  if (!job.output_dir) return;
  const runKind = job.run_kind || 'news';
  const detail = await (await api(`/api/runs/${runKind}/${job.output_dir}`)).json();
  const report = detail.files.find((file) => file.name === 'news_summary.md');
  const sources = detail.files.find((file) => file.name.endsWith('_sources.csv'));
  $('newsPreview').className = 'panel preview';
  if (!report) {
    $('newsPreview').innerHTML = '<div class="empty">News summary finished, but no report file was found.</div>';
    return;
  }
  const preview = await (await api(`/api/preview/${runKind}/${job.output_dir}/${report.name}`)).json();
  $('newsPreview').innerHTML = `
    <div class="panel-title-row">
      <a class="download" href="${report.url}" target="_blank">Open news summary</a>
      ${sources ? `<a class="download" href="${sources.url}" target="_blank">Open sources</a>` : ''}
    </div>
    ${renderMarkdown(preview.text || '')}
  `;
}

async function showRunReportFromJob(job) {
  if (!job.output_dir || !['scan', 'backtest'].includes(job.command) || !$('runReportPreview')) return;
  const runKind = job.run_kind || (job.command === 'scan' ? 'scans' : 'backtests');
  const reportName = job.command === 'scan' ? 'portfolio_report.md' : 'backtest_report.md';
  try {
    const detail = await (await api(`/api/runs/${runKind}/${job.output_dir}`)).json();
    const report = detail.files.find((file) => file.name === reportName);
    if (!report) {
      $('runReportPreview').className = 'panel preview empty';
      $('runReportPreview').textContent = `${job.command === 'scan' ? 'Scan' : 'Backtest'} finished, but ${reportName} was not found.`;
      return;
    }
    const preview = await (await api(`/api/preview/${runKind}/${job.output_dir}/${report.name}`)).json();
    $('runReportPreview').className = 'panel preview';
    $('runReportPreview').innerHTML = `
      <div class="panel-title-row">
        <h3>${job.command === 'scan' ? 'Current Scan Report' : 'Backtest Report'}</h3>
        <a class="download" href="${report.url}" target="_blank">Open report</a>
      </div>
      <div class="markdown-body">${renderMarkdown(preview.text || '')}</div>
    `;
  } catch (error) {
    $('runReportPreview').className = 'panel preview empty';
    $('runReportPreview').textContent = `Could not load ${job.command} report: ${error.message}`;
  }
}

async function startAgentFromQuestion(question, ticker = '') {
  const query = question.trim();
  const status = $('agentStatus');
  if (!query) {
    status.textContent = 'Type a market question first.';
    return;
  }
  $('askAgent').disabled = true;
  $('askAgent').textContent = 'Researching...';
  $('agentQuestion').value = '';
  status.textContent = 'Starting agent session...';
  $('agentConversation').className = 'agent-conversation';
  $('agentConversation').innerHTML = chatMessageBubble({ role: 'user', content: query, created_at: new Date().toISOString() });
  try {
    const session = await (await api('/api/agent/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker, query }),
    })).json();
    state.agentSessionId = session.id;
    localStorage.setItem('marketSignalScanner.agentSessionId', session.id);
    renderAgentSession(session);
    renderLatestActivity({ ...session, command: 'agent', title: session.ticker ? `agent ${session.ticker}` : 'agent research', run_kind: 'agents' });
    await loadActivity();
    startAgentPolling(session.id);
  } catch (error) {
    status.textContent = `Could not start agent: ${error.message}`;
    $('askAgent').disabled = false;
    $('askAgent').textContent = 'Ask';
  }
}

async function restoreAgentSession() {
  if (state.agentPollTimer || state.agentSessionId) return;
  const savedId = localStorage.getItem('marketSignalScanner.agentSessionId');
  let sessionId = null;
  try {
    sessionId = await fetchLatestSessionId('agent', savedId);
    if (!sessionId) return;
    const session = await (await api(`/api/agent/sessions/${sessionId}`)).json();
    state.agentSessionId = session.id;
    localStorage.setItem('marketSignalScanner.agentSessionId', session.id);
    renderAgentSession(session);
    if (!isTerminalStatus(session.status)) startAgentPolling(session.id);
  } catch (error) {
    if (savedId === sessionId) localStorage.removeItem('marketSignalScanner.agentSessionId');
    $('agentStatus').textContent = `Could not restore agent session: ${error.message}`;
  }
}

function startAgentPolling(sessionId) {
  if (state.agentPollTimer) clearInterval(state.agentPollTimer);
  state.agentPollTimer = setInterval(async () => {
    try {
      const session = await (await api(`/api/agent/sessions/${sessionId}`)).json();
      renderAgentSession(session);
      renderLatestActivity({ ...session, command: 'agent', title: session.ticker ? `agent ${session.ticker}` : 'agent research', run_kind: 'agents', logs: formatEventsAsLog(session.events || []) });
      if (isTerminalStatus(session.status)) {
        clearInterval(state.agentPollTimer);
        state.agentPollTimer = null;
        await loadRuns();
        await loadActivity();
      }
    } catch (error) {
      $('agentStatus').textContent = `Agent polling failed: ${error.message}`;
      clearInterval(state.agentPollTimer);
    }
  }, 1500);
}

function renderAgentSession(session) {
  localStorage.setItem('marketSignalScanner.agentSessionId', session.id);
  $('agentSessionBadge').className = `badge ${session.status}`;
  $('agentSessionBadge').textContent = session.status;
  const out = session.output_dir && isTerminalStatus(session.status) ? `Output: agents/${session.output_dir}.` : '';
  const err = session.error ? `Agent ${session.status}: ${session.error}.` : '';
  $('agentStatus').textContent = err || out;
  renderAgentConversation(session);
  $('askAgent').disabled = !isTerminalStatus(session.status);
  $('askAgent').textContent = isTerminalStatus(session.status) ? 'Ask' : 'Researching...';
  if (isTerminalStatus(session.status)) {
    if (session.status !== 'completed') {
      state.agentSessionId = null;
      localStorage.removeItem('marketSignalScanner.agentSessionId');
    }
  }
}

function renderAgentConversation(session) {
  renderConversation('agentConversation', session.events || [], session.messages || [], session.status, 'Research session', 'Agent finished. Ask a follow-up below.', agentReportBubble(session));
}

function renderConversation(containerId, events, messages, status, title, completedText = 'Finished.', finalBubble = '') {
  const box = $(containerId);
  if (!events.length && !messages.length) {
    box.className = 'agent-conversation empty';
    box.innerHTML = `<div class="chat-day">No ${escapeHtml(title)} started yet.</div>`;
    return;
  }
  box.className = 'agent-conversation';
  const parts = [`<div class="chat-day">${escapeHtml(title)}</div>`];
  if (messages.length) {
    parts.push(chatMessageBubble(messages[0]));
  }
  parts.push(...events.map((event) => agentEventBubble(event)));
  if (finalBubble) parts.push(finalBubble);
  if (messages.length > 1) {
    parts.push('<div class="chat-day">Follow-up</div>');
    parts.push(...messages.slice(1).map((message) => chatMessageBubble(message)));
  }
  if (!messages.length && isTerminalStatus(status)) {
    const text = status === 'completed' ? completedText : `${title} ${status}.`;
    parts.push(`<div class="chat-day">${escapeHtml(text)}</div>`);
  }
  box.innerHTML = parts.join('');
  box.scrollTop = box.scrollHeight;
}

function agentReportBubble(session) {
  if (!session.report || !session.output_dir) return '';
  const reportUrl = `/api/files/agents/${session.output_dir}/agent_report.md`;
  const sourcesUrl = `/api/files/agents/${session.output_dir}/agent_sources.csv`;
  const logUrl = `/api/files/agents/${session.output_dir}/agent_log.md`;
  return `
    <div class="chat-row incoming">
      <div class="agent-bubble report">
        <div class="bubble-name">Agent</div>
        <div class="bubble-text">
          ${renderMarkdown(session.report)}
          <div class="bubble-links">
            <a href="${reportUrl}" target="_blank">Open agent report</a>
            <a href="${sourcesUrl}" target="_blank">Open sources</a>
            <a href="${logUrl}" target="_blank">Open log</a>
          </div>
        </div>
        <div class="bubble-time">${escapeHtml(formatEventTime(session.finished_at))}</div>
      </div>
    </div>
  `;
}

function agentEventBubble(event) {
  const kind = event.kind || 'thought';
  const message = String(event.message || '');
  const expanded = expandableText(message, 220);
  return `
    <div class="chat-row incoming">
      <div class="agent-bubble ${escapeHtml(kind)}">
        <div class="bubble-name">${escapeHtml(agentEventLabel(kind))}</div>
        <div class="bubble-text">${expanded}</div>
        <div class="bubble-time">${escapeHtml(formatEventTime(event.created_at))}</div>
      </div>
    </div>
  `;
}

function expandableText(text, limit) {
  if (text.length <= limit) return `<span>${escapeHtml(text)}</span>`;
  const preview = text.slice(0, limit).trimEnd();
  return `
    <details>
      <summary>${escapeHtml(preview)}... <span>show more</span></summary>
      <div class="bubble-full">${escapeHtml(text)}</div>
    </details>
  `;
}

function agentEventLabel(kind) {
  if (kind === 'action') return 'Research Agent';
  if (kind === 'observation') return 'Sources';
  return 'Agent';
}

function formatEventTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' });
}

function chatMessageBubble(message) {
  const role = message.role || 'assistant';
  const outgoing = role === 'user';
  const content = role === 'assistant' ? renderMarkdown(message.content || '') : escapeHtml(message.content || '');
  return `
    <div class="chat-row ${outgoing ? 'outgoing' : 'incoming'}">
      <div class="chat-message ${escapeHtml(role)}">
        <div class="bubble-name">${outgoing ? 'You' : 'Agent'}</div>
        <div class="bubble-text">${content}</div>
        <div class="bubble-time">${escapeHtml(formatEventTime(message.created_at))}</div>
      </div>
    </div>
  `;
}

function appendChatMessage(containerId, message) {
  const box = $(containerId);
  if (!box) return;
  if (box.classList.contains('empty')) {
    box.className = 'agent-conversation';
    box.innerHTML = '<div class="chat-day">Research session</div>';
  }
  box.insertAdjacentHTML('beforeend', chatMessageBubble(message));
  box.scrollTop = box.scrollHeight;
}

function resetAgentConversation() {
  state.agentSessionId = null;
  localStorage.removeItem('marketSignalScanner.agentSessionId');
  if (state.agentPollTimer) {
    clearInterval(state.agentPollTimer);
    state.agentPollTimer = null;
  }
  $('agentSessionBadge').className = 'badge queued';
  $('agentSessionBadge').textContent = 'idle';
  $('agentConversation').className = 'agent-conversation empty';
  $('agentConversation').innerHTML = '<div class="chat-day">No Research session started yet.</div>';
  $('agentQuestion').value = '';
  $('askAgent').disabled = false;
  $('askAgent').textContent = 'Ask';
  $('agentStatus').textContent = '';
}

async function askAgentQuestion() {
  const question = $('agentQuestion').value.trim();
  if (!question) return;
  if (!state.agentSessionId) {
    await startAgentFromQuestion(question);
    return;
  }
  $('agentQuestion').value = '';
  appendChatMessage('agentConversation', { role: 'user', content: question, created_at: new Date().toISOString() });
  $('askAgent').disabled = true;
  $('agentStatus').textContent = 'Asking follow-up question...';
  try {
    const session = await (await api(`/api/agent/sessions/${state.agentSessionId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question }),
    })).json();
    renderAgentSession(session);
  } catch (error) {
    $('agentStatus').textContent = `Follow-up failed: ${error.message}`;
    appendChatMessage('agentConversation', { role: 'assistant', content: `Follow-up failed: ${error.message}`, created_at: new Date().toISOString() });
  } finally {
    $('askAgent').disabled = false;
    $('askAgent').textContent = 'Ask';
  }
}

async function startTrendCatcherFromUi() {
  $('runTrendCatcher').disabled = true;
  $('runTrendCatcher').textContent = 'Trend Catcher Running...';
  $('trendCatcherStatus').textContent = 'Starting Trend Catcher scan...';
  $('trendCatcherConversation').className = 'agent-conversation';
  $('trendCatcherConversation').innerHTML = '';
  $('trendCatcherReport').className = 'panel preview empty';
  $('trendCatcherReport').textContent = 'Trend Catcher is scanning markets...';
  try {
    const session = await (await api('/api/trend-catcher/sessions', { method: 'POST' })).json();
    state.trendCatcherSessionId = session.id;
    localStorage.setItem('marketSignalScanner.trendCatcherSessionId', session.id);
    renderTrendCatcherSession(session);
    renderLatestActivity({ ...session, command: 'trend-catcher', title: 'trend-catcher', run_kind: 'trend-catcher' });
    await loadActivity();
    startTrendCatcherPolling(session.id);
  } catch (error) {
    $('trendCatcherStatus').textContent = `Could not start Trend Catcher: ${error.message}`;
    $('runTrendCatcher').disabled = false;
    $('runTrendCatcher').textContent = 'Run Trend Catcher';
  }
}

async function restoreTrendCatcherSession() {
  if (state.trendCatcherPollTimer || state.trendCatcherSessionId) return;
  const savedId = localStorage.getItem('marketSignalScanner.trendCatcherSessionId');
  let sessionId = null;
  try {
    sessionId = await fetchLatestSessionId('trend-catcher', savedId);
    if (!sessionId) return;
    const session = await (await api(`/api/trend-catcher/sessions/${sessionId}`)).json();
    state.trendCatcherSessionId = session.id;
    localStorage.setItem('marketSignalScanner.trendCatcherSessionId', session.id);
    renderTrendCatcherSession(session);
    if (!isTerminalStatus(session.status)) startTrendCatcherPolling(session.id);
  } catch (error) {
    if (savedId === sessionId) localStorage.removeItem('marketSignalScanner.trendCatcherSessionId');
    $('trendCatcherStatus').textContent = `Could not restore Trend Catcher session: ${error.message}`;
  }
}

function startTrendCatcherPolling(sessionId) {
  if (state.trendCatcherPollTimer) clearInterval(state.trendCatcherPollTimer);
  const pollTrendCatcher = async () => {
    try {
      const session = await (await api(`/api/trend-catcher/sessions/${sessionId}`)).json();
      renderTrendCatcherSession(session);
      renderLatestActivity({ ...session, command: 'trend-catcher', title: 'trend-catcher', run_kind: 'trend-catcher', logs: formatEventsAsLog(session.events || []) });
      if (isTerminalStatus(session.status)) {
        clearInterval(state.trendCatcherPollTimer);
        state.trendCatcherPollTimer = null;
        await loadRuns();
        await loadActivity();
      }
    } catch (error) {
      $('trendCatcherStatus').textContent = `Trend Catcher polling failed: ${error.message}`;
      clearInterval(state.trendCatcherPollTimer);
    }
  };
  pollTrendCatcher();
  state.trendCatcherPollTimer = setInterval(pollTrendCatcher, 1500);
}

function renderTrendCatcherSession(session) {
  localStorage.setItem('marketSignalScanner.trendCatcherSessionId', session.id);
  $('trendCatcherSessionBadge').className = `badge ${session.status}`;
  $('trendCatcherSessionBadge').textContent = session.status;
  const out = session.output_dir ? ` Output: trend-catcher/${session.output_dir}.` : '';
  const err = session.error ? ` Error: ${session.error}.` : '';
  $('trendCatcherStatus').textContent = `Trend Catcher ${session.status}.${out}${err}`;
  $('runTrendCatcher').disabled = !isTerminalStatus(session.status);
  $('runTrendCatcher').textContent = isTerminalStatus(session.status) ? 'Run Trend Catcher' : 'Trend Catcher Running...';
  renderConversation('trendCatcherConversation', session.events || [], [], session.status, 'Trend Catcher scan');
  if (session.report) {
    $('trendCatcherReport').className = 'panel preview';
    const reportLink = session.output_dir ? `<a class="download" href="/api/files/trend-catcher/${session.output_dir}/trend_catcher_report.md" target="_blank">Open Trend Catcher report</a>` : '';
    const sourceLink = session.output_dir ? `<a class="download" href="/api/files/trend-catcher/${session.output_dir}/trend_catcher_sources.csv" target="_blank">Open sources</a>` : '';
    const pulseLink = session.output_dir ? `<a class="download" href="/api/files/trend-catcher/${session.output_dir}/trend_catcher_market_pulse.csv" target="_blank">Open market pulse</a>` : '';
    const logLink = session.output_dir ? `<a class="download" href="/api/files/trend-catcher/${session.output_dir}/trend_catcher_log.md" target="_blank">Open log</a>` : '';
    $('trendCatcherReport').innerHTML = `<div class="panel-title-row">${reportLink}${sourceLink}${pulseLink}${logLink}</div>${renderMarkdown(session.report)}`;
  }
  if (isTerminalStatus(session.status)) {
    state.trendCatcherSessionId = null;
  }
}

async function loadConfig() {
  try {
    const text = await (await api('/api/config')).text();
    $('configEditor').value = text;
    $('configStatus').textContent = 'Config loaded.';
    loadConfigTickers();
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
    refreshThemeStylesheet();
    await loadChartTickerChoices(true);
    await loadConfigTickers();
    $('configStatus').textContent = 'Saved config/config.yaml.';
  } catch (error) {
    $('configStatus').textContent = `Save failed: ${error.message}`;
  }
}

async function searchTickerDiscovery(mode = 'quick') {
  state.tickerDiscoveryMode = mode;
  const query = $('tickerDiscoveryQuery').value.trim();
  const status = $('tickerDiscoveryStatus');
  const box = $('tickerDiscoveryResults');
  if (!query) {
    status.textContent = 'Enter a theme or search phrase first.';
    return;
  }
  const isDeep = mode === 'deep';
  status.textContent = isDeep ? 'Running deep research search. This may take a minute...' : 'Searching for ticker ideas...';
  box.className = 'ticker-discovery-results empty';
  box.textContent = isDeep ? 'Searching the web, reading sources, and asking the LLM to extract public tickers...' : 'Searching...';
  try {
    const data = await (await api(isDeep ? '/api/ticker-discovery/deep' : '/api/ticker-discovery', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, max_results: 24 }),
    })).json();
    const sourceNote = data.sources_reviewed ? ` Sources reviewed: ${data.sources_reviewed}.` : '';
    status.textContent = `${data.count} candidate ticker(s) found.${sourceNote} These are watchlist ideas, not recommendations.`;
    state.tickerDiscoveryCandidates = data.candidates || [];
    renderTickerDiscoveryResults(state.tickerDiscoveryCandidates);
  } catch (error) {
    status.textContent = `${isDeep ? 'Deep research search' : 'Ticker search'} failed: ${error.message}`;
    box.className = 'ticker-discovery-results empty';
    box.textContent = 'No ticker ideas loaded.';
    state.tickerDiscoveryCandidates = [];
  }
}

function renderTickerDiscoveryResults(candidates) {
  const box = $('tickerDiscoveryResults');
  if (!box) return;
  if (!candidates.length) {
    box.className = 'ticker-discovery-results empty';
    box.textContent = 'No candidates found. Try a broader theme like top dividend companies, income ETFs, energy, water, AI, uranium, or cybersecurity.';
    return;
  }
  box.className = 'ticker-discovery-results';
  box.innerHTML = candidates.map((item) => `
    <article class="ticker-card ${item.already_configured ? 'already-configured' : ''}">
      <div class="ticker-card-head">
        <div>
          <strong>${escapeHtml(item.ticker)}</strong>
          <span>${escapeHtml(item.name || item.ticker)}</span>
        </div>
        <em>${escapeHtml(item.asset_type || 'Unknown')}</em>
      </div>
      <p>${escapeHtml(item.reason || '')}</p>
      <div class="ticker-card-meta">
        <span>${escapeHtml(item.source || 'search')}</span>
        ${item.confidence ? `<span>${escapeHtml(item.confidence)} confidence</span>` : ''}
        ${item.exchange ? `<span>${escapeHtml(item.exchange)}</span>` : ''}
      </div>
      ${Array.isArray(item.sources) && item.sources.length ? `<div class="ticker-card-sources">${item.sources.map((source, index) => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">Source ${index + 1}</a>`).join('')}</div>` : ''}
      ${item.already_configured
        ? `<button class="danger-button small" type="button" data-remove-discovery-ticker="${escapeHtml(item.ticker)}">Remove From Config</button>`
        : `<button class="primary small" type="button" data-add-ticker="${escapeHtml(item.ticker)}">Add To Config</button>`}
    </article>
  `).join('');
}

async function addTickerToConfig(ticker) {
  const status = $('tickerDiscoveryStatus');
  status.textContent = `Adding ${ticker} to config...`;
  try {
    const result = await updateConfigTickers('/api/config/tickers/add', [ticker]);
    markTickerDiscoveryCandidateTracked(ticker);
    status.textContent = `${result.message || `Added ${ticker}.`} You can add more from the same results.`;
    await refreshConfigAfterTickerChange();
    renderTickerDiscoveryResults(state.tickerDiscoveryCandidates);
  } catch (error) {
    status.textContent = `Could not add ${ticker}: ${error.message}`;
  }
}

function markTickerDiscoveryCandidateTracked(ticker) {
  const normalized = String(ticker || '').trim().toUpperCase();
  state.tickerDiscoveryCandidates = (state.tickerDiscoveryCandidates || []).map((item) => (
    String(item.ticker || '').trim().toUpperCase() === normalized
      ? { ...item, already_configured: true }
      : item
  ));
}

async function removeTickerFromConfig(ticker) {
  const confirmed = window.confirm(`Remove ${ticker} from config.yaml? It will stop appearing in scans unless it also comes from an enabled group like sp500 or crypto_top.`);
  if (!confirmed) return;
  const status = $('configStatus');
  status.textContent = `Removing ${ticker} from config...`;
  try {
    const result = await updateConfigTickers('/api/config/tickers/remove', [ticker]);
    status.textContent = result.message || `Removed ${ticker}.`;
    await refreshConfigAfterTickerChange();
  } catch (error) {
    status.textContent = `Could not remove ${ticker}: ${error.message}`;
  }
}

async function removeTickerFromDiscovery(ticker) {
  const confirmed = window.confirm(`Remove ${ticker} from config.yaml?`);
  if (!confirmed) return;
  const status = $('tickerDiscoveryStatus');
  status.textContent = `Removing ${ticker} from config...`;
  try {
    const result = await updateConfigTickers('/api/config/tickers/remove', [ticker]);
    markTickerDiscoveryCandidateUntracked(ticker);
    status.textContent = `${result.message || `Removed ${ticker}.`} You can continue reviewing these results.`;
    await refreshConfigAfterTickerChange();
    renderTickerDiscoveryResults(state.tickerDiscoveryCandidates);
  } catch (error) {
    status.textContent = `Could not remove ${ticker}: ${error.message}`;
  }
}

function markTickerDiscoveryCandidateUntracked(ticker) {
  const normalized = String(ticker || '').trim().toUpperCase();
  state.tickerDiscoveryCandidates = (state.tickerDiscoveryCandidates || []).map((item) => (
    String(item.ticker || '').trim().toUpperCase() === normalized
      ? { ...item, already_configured: false }
      : item
  ));
}

async function updateConfigTickers(path, tickers) {
  const response = await api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tickers }),
  });
  return response.json();
}

async function refreshConfigAfterTickerChange() {
  await loadConfig();
  await loadConfigTickers();
  await loadChartTickerChoices(true);
}

async function loadConfigTickers() {
  const box = $('configTickerList');
  if (!box) return;
  try {
    const data = await (await api('/api/config/tickers')).json();
    renderConfigTickerList(data.tickers || []);
  } catch (error) {
    box.className = 'config-ticker-list empty';
    box.textContent = `Could not load configured tickers: ${error.message}`;
  }
}

function renderConfigTickerList(tickers) {
  const box = $('configTickerList');
  if (!box) return;
  if (!tickers.length) {
    box.className = 'config-ticker-list empty';
    box.textContent = 'No manual tickers are configured.';
    return;
  }
  box.className = 'config-ticker-list';
  box.innerHTML = tickers.map((item) => `
    <article class="config-ticker-row">
      <div>
        <strong>${escapeHtml(item.ticker)}</strong>
        <span>${escapeHtml(item.name || 'No company name from latest scan yet')}</span>
        <em>${escapeHtml(item.summary || '')}</em>
      </div>
      <button class="danger-button small" type="button" data-remove-ticker="${escapeHtml(item.ticker)}">Remove</button>
    </article>
  `).join('');
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
    notes.push('The configured LLM is ready for News Summary and Agent research.');
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

async function runLlmDiagnostic(kind) {
  const status = $('llmDiagnosticStatus');
  const result = $('llmDiagnosticResult');
  const simpleButton = $('runSimpleLlmTest');
  const toolButton = $('runToolLlmTest');
  status.textContent = `Running ${kind === 'simple' ? 'simple query' : 'tool format'} diagnostic...`;
  simpleButton.disabled = true;
  toolButton.disabled = true;
  try {
    const data = await (await api('/api/llm/diagnostic', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind }),
    })).json();
    renderLlmDiagnostic(data);
  } catch (error) {
    result.className = 'llm-diagnostic-grid';
    result.innerHTML = `
      <div class="diagnostic-box">
        <h4>Error</h4>
        <pre>${escapeHtml(error.message)}</pre>
      </div>
    `;
    status.textContent = 'Diagnostic request failed.';
  } finally {
    simpleButton.disabled = false;
    toolButton.disabled = false;
  }
}

function renderLlmDiagnostic(data) {
  const check = data.format_check || {};
  const ok = data.ok && check.ok;
  const rawOutput = typeof data.raw_output === 'string'
    ? data.raw_output
    : JSON.stringify(data.raw_output || { error: data.error || 'No raw output.' }, null, 2);
  $('llmDiagnosticStatus').innerHTML = `
    <span class="badge ${ok ? 'completed' : 'failed'}">${ok ? 'passed' : 'check output'}</span>
    ${escapeHtml(check.message || data.error || 'Diagnostic finished.')}
  `;
  $('llmDiagnosticResult').className = 'llm-diagnostic-grid';
  $('llmDiagnosticResult').innerHTML = `
    <div class="diagnostic-box diagnostic-input">
      <h4>Raw Input To LLM</h4>
      <pre>${escapeHtml(JSON.stringify(data.raw_input || {}, null, 2))}</pre>
    </div>
    <div class="diagnostic-results">
      <div class="diagnostic-box diagnostic-output">
        <h4>Raw Output From LLM</h4>
        <pre>${escapeHtml(rawOutput)}</pre>
      </div>
      <div class="diagnostic-box diagnostic-model-text">
        <h4>Model Text</h4>
        <pre>${escapeHtml(data.model_text || '')}</pre>
      </div>
      <div class="diagnostic-box diagnostic-format-check">
        <h4>Format Check</h4>
        <pre>${escapeHtml(JSON.stringify(data.format_check || {}, null, 2))}</pre>
      </div>
    </div>
  `;
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
  wireTabLinks();
  wireInteractiveChart();
  wireOpportunityMap();
  wireNewsAction();
  loadGuardrailPreferences();
  $('runTrendCatcher').addEventListener('click', startTrendCatcherFromUi);
  $('resetAgent').addEventListener('click', resetAgentConversation);
  $('askAgent').addEventListener('click', askAgentQuestion);
  $('agentSuggestions').addEventListener('click', (event) => {
    const button = event.target.closest('[data-question]');
    if (button) useAgentSuggestion(button.dataset.question || '');
  });
  $('agentQuestion').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') askAgentQuestion();
  });
  $('runScan').addEventListener('click', () => createJob({ command: 'scan', skip_fundamentals: $('scanSkipFundamentals').checked }));
  $('runBacktest').addEventListener('click', () => createJob({ command: 'backtest' }));
  $('refreshOpportunityMap').addEventListener('click', loadOpportunityMap);
  $('refreshGuardrails').addEventListener('click', loadGuardrails);
  ['guardrailBudget', 'riskToleranceSlider', 'timeHorizonSlider', 'fomoBrakeSlider', 'maxIdeaPctSlider'].forEach((id) => {
    $(id).addEventListener('change', updateGuardrailPreferencesFromUi);
    $(id).addEventListener('input', updateGuardrailPreferencesFromUi);
  });
  $('resetGuardrailPrefs').addEventListener('click', resetGuardrailPreferences);
  ['guardrailResearch', 'guardrailFomo', 'guardrailSellReview', 'guardrailSleep'].forEach((id) => {
    $(id).addEventListener('click', (event) => {
      const target = event.target.closest('[data-ticker]');
      if (target) selectGuardrailItem(target.dataset.ticker, target.dataset.mode);
    });
  });
  $('guardrailDetail').addEventListener('input', (event) => {
    if (event.target.id === 'guardrailNoteEditor') {
      localStorage.setItem(event.target.dataset.noteKey, event.target.value);
    }
  });
  document.body.addEventListener('click', (event) => {
    const chartButton = event.target.closest('[data-open-chart]');
    if (chartButton) {
      openTickerChart(chartButton.dataset.openChart);
      return;
    }
    const newsButton = event.target.closest('[data-run-news]');
    if (newsButton) {
      runTickerNews(newsButton.dataset.runNews);
      return;
    }
    const agentButton = event.target.closest('[data-agent-research]');
    if (agentButton) {
      startDecisionAgent(agentButton.dataset.agentResearch);
    }
  });
  $('opportunityLeaderboard').addEventListener('click', (event) => {
    const target = event.target.closest('[data-ticker]');
    if (target) selectOpportunityTicker(target.dataset.ticker);
  });
  $('opportunityHeatmap').addEventListener('click', (event) => {
    const target = event.target.closest('[data-ticker]');
    if (target) selectOpportunityTicker(target.dataset.ticker);
  });
  $('loadInteractiveChart').addEventListener('click', loadInteractiveChart);
  $('resetInteractiveChart').addEventListener('click', resetInteractiveChart);
  $('chartPeriod').addEventListener('change', () => {
    applyPeriodDefaults();
    loadInteractiveChart();
  });
  ['chartInterval', 'chartType', 'showBollinger', 'showLevels', 'showTrendlines', 'showVolume', 'showRsi', 'showMacd'].forEach((id) => {
    $(id).addEventListener('change', loadInteractiveChart);
  });
  $('chartMouseMode').addEventListener('change', () => {
    updateChartMouseMode();
    drawInteractiveChart();
  });
  ['showSma', 'showEma'].forEach((id) => {
    $(id).addEventListener('change', drawInteractiveChart);
  });
  $('chartTicker').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') loadInteractiveChart();
  });
  $('chartTicker').addEventListener('input', () => {
    syncChartTickerSelect($('chartTicker').value);
  });
  $('chartTickerSelect').addEventListener('change', () => {
    const ticker = $('chartTickerSelect').value;
    if (!ticker) return;
    $('chartTicker').value = ticker;
    loadInteractiveChart();
  });
  $('runChart').addEventListener('click', () => createJob({
    command: 'chart',
    ticker: $('chartTicker').value.trim(),
    period: $('chartPeriod').value.trim(),
    interval: $('chartInterval').value.trim(),
    chart_type: $('chartType').value,
    lookback: Number($('chartLookback').value || 180),
    moving_averages: $('chartMa').value.trim() || '20,50,100,200',
    no_support_resistance: !($('showLevels').checked || $('showTrendlines').checked),
    no_bollinger: !$('showBollinger').checked,
    no_volume: !$('showVolume').checked,
    no_rsi: !$('showRsi').checked,
    no_macd: !$('showMacd').checked,
  }));
  $('refreshRunProgress').addEventListener('click', loadRunProgress);
  $('refreshRuns').addEventListener('click', loadRuns);
  document.querySelectorAll('[data-delete-kind]:not([data-delete-run])').forEach((button) => {
    button.addEventListener('click', () => deleteRunsForKind(button.dataset.deleteKind));
  });
  $('refreshActivityPage').addEventListener('click', loadActivity);
  $('refreshLlm').addEventListener('click', loadLlmStatus);
  $('startLlm').addEventListener('click', startLlm);
  $('stopLlm').addEventListener('click', stopLlm);
  $('runSimpleLlmTest').addEventListener('click', () => runLlmDiagnostic('simple'));
  $('runToolLlmTest').addEventListener('click', () => runLlmDiagnostic('tool'));
  $('loadConfig').addEventListener('click', loadConfig);
  $('saveConfig').addEventListener('click', saveConfig);
  $('searchTickers').addEventListener('click', () => searchTickerDiscovery('quick'));
  $('deepSearchTickers').addEventListener('click', () => searchTickerDiscovery('deep'));
  $('tickerDiscoveryQuery').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') searchTickerDiscovery('quick');
  });
  $('refreshConfigTickers').addEventListener('click', loadConfigTickers);
  $('tickerDiscoveryResults').addEventListener('click', (event) => {
    const addButton = event.target.closest('[data-add-ticker]');
    if (addButton) addTickerToConfig(addButton.dataset.addTicker);
    const removeButton = event.target.closest('[data-remove-discovery-ticker]');
    if (removeButton) removeTickerFromDiscovery(removeButton.dataset.removeDiscoveryTicker);
  });
  $('configTickerList').addEventListener('click', (event) => {
    const button = event.target.closest('[data-remove-ticker]');
    if (button) removeTickerFromConfig(button.dataset.removeTicker);
  });
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
loadActivity();
loadRunProgress();
loadConfig();
loadLlmStatus();
startActivityPolling();
