/* FreightPOP AI Metrics dashboard — renders site/metrics.json (static, no build step) */

const USAGE = getComputedStyle(document.documentElement).getPropertyValue('--series-usage').trim() || '#4088cf';
const RATE = getComputedStyle(document.documentElement).getPropertyValue('--series-rate').trim() || '#c96a24';

const fmt = {
  int: v => v == null ? '—' : v.toLocaleString('en-US'),
  num: (v, d = 1) => v == null ? '—' : Number(v).toFixed(d),
  pct: v => v == null ? '—' : (v * 100).toFixed(1) + '%',
  ms: v => v == null ? '—' : (v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms'),
};

function deltaBadge(cur, prev, { goodWhenUp = true, isPct = false } = {}) {
  if (cur == null || prev == null || prev === 0) return '';
  const change = (cur - prev) / Math.abs(prev);
  if (!isFinite(change)) return '';
  const up = change >= 0.005, down = change <= -0.005;
  const cls = !up && !down ? 'flat' : (up === goodWhenUp ? 'good' : 'bad');
  const arrow = up ? '▲' : down ? '▼' : '·';
  const label = isPct
    ? `${change >= 0 ? '+' : ''}${((cur - prev) * 100).toFixed(1)}pp`
    : `${change >= 0 ? '+' : ''}${(change * 100).toFixed(0)}%`;
  return `<span class="delta ${cls}">${arrow} ${label} vs prior 7d</span>`;
}

function tile({ label, value, sub = '', gap = null }) {
  if (gap) {
    return `<div class="tile gap"><div class="label">${label}</div>
      <div class="value">Instrumentation gap</div><div class="sub">${gap}</div></div>`;
  }
  return `<div class="tile"><div class="label">${label}</div>
    <div class="value">${value}</div><div class="sub">${sub}</div></div>`;
}

/* ---------- charts ---------- */

Chart.defaults.font.family = "'Nunito', sans-serif";
Chart.defaults.font.size = 11;
Chart.defaults.color = '#8a8a8a';

function seriesTable(series, valueFmt) {
  const rows = series.slice(-14).map(([d, v]) => `<tr><td>${d}</td><td>${valueFmt(v)}</td></tr>`).join('');
  return `<details><summary>View data (last 14 days)</summary>
    <table><thead><tr><th>Date (UTC)</th><th>Value</th></tr></thead><tbody>${rows}</tbody></table></details>`;
}

function lineChartCard(containerId, { title, series, color = USAGE, isPct = false, valueFmt }) {
  valueFmt = valueFmt || (isPct ? fmt.pct : fmt.int);
  const id = title.toLowerCase().replace(/[^a-z0-9]+/g, '-');
  const card = document.createElement('div');
  card.className = 'fp-card';
  card.innerHTML = `<h3>${title}</h3><div class="chart-wrap"><canvas id="c-${id}"></canvas></div>
    ${seriesTable(series, valueFmt)}`;
  document.getElementById(containerId).appendChild(card);
  new Chart(document.getElementById(`c-${id}`), {
    type: 'line',
    data: {
      labels: series.map(p => p[0]),
      datasets: [{ data: series.map(p => p[1]), borderColor: color, borderWidth: 2,
                   pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: color,
                   tension: 0.25, fill: false }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: ctx => ` ${valueFmt(ctx.parsed.y)}` } },
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 6, maxRotation: 0 } },
        y: { beginAtZero: true, border: { display: false },
             grid: { color: 'rgba(0,0,0,0.05)' },
             ticks: { maxTicksLimit: 5, callback: v => isPct ? (v * 100).toFixed(0) + '%' : v } },
      },
    },
  });
}

function barChartCard(containerId, { title, rows, valueFmt = fmt.int }) {
  const id = title.toLowerCase().replace(/[^a-z0-9]+/g, '-');
  const card = document.createElement('div');
  card.className = 'fp-card';
  const tableRows = rows.map(r => `<tr><td>${r.label}</td><td>${valueFmt(r.value)}</td><td>${fmt.pct(r.share)}</td></tr>`).join('');
  card.innerHTML = `<h3>${title}</h3><div class="chart-wrap"><canvas id="c-${id}"></canvas></div>
    <details><summary>View data</summary><table><thead><tr><th>Page</th><th>Sessions</th><th>Share</th></tr></thead>
    <tbody>${tableRows}</tbody></table></details>`;
  document.getElementById(containerId).appendChild(card);
  new Chart(document.getElementById(`c-${id}`), {
    type: 'bar',
    data: {
      labels: rows.map(r => r.label),
      datasets: [{ data: rows.map(r => r.value), backgroundColor: USAGE,
                   borderRadius: 4, barThickness: 18 }],
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => ` ${valueFmt(ctx.parsed.x)} sessions` } } },
      scales: {
        x: { beginAtZero: true, border: { display: false }, grid: { color: 'rgba(0,0,0,0.05)' }, ticks: { maxTicksLimit: 5 } },
        y: { grid: { display: false } },
      },
    },
  });
}

function sparkline(canvas, values) {
  new Chart(canvas, {
    type: 'line',
    data: { labels: values.map((_, i) => i), datasets: [{ data: values, borderColor: USAGE, borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: false }] },
    options: { responsive: false, animation: false, plugins: { legend: { display: false }, tooltip: { enabled: false } },
      scales: { x: { display: false }, y: { display: false, beginAtZero: true } } },
  });
}

/* ---------- sections ---------- */

function renderSourcePills(status) {
  const el = document.getElementById('source-pills');
  el.innerHTML = Object.entries(status).map(([name, s]) => {
    let cls = 'pill-mock', text = `${name} · mock`;
    if (s.mode === 'live') {
      if (s.last_error) { cls = 'pill-error'; text = `${name} · error`; }
      else if ((s.stale_days ?? 0) > 1) { cls = 'pill-stale'; text = `${name} · stale ${s.stale_days}d`; }
      else { cls = 'pill-live'; text = `${name} · live`; }
    }
    const tip = s.last_error ? ` title="${String(s.last_error).replace(/"/g, '&quot;')}"` : '';
    return `<span class="pill ${cls}"${tip}><span class="dot"></span>${text}</span>`;
  }).join('');
}

function renderAdoption(m) {
  const a = m.adoption.current;
  document.getElementById('adoption-tiles').innerHTML = [
    a.active_pct_of_seats_30d == null
      ? tile({ label: 'Active users % of seats (30d)', gap: 'needs enablement roster' })
      : tile({ label: 'Active users % of seats (30d)', value: fmt.pct(a.active_pct_of_seats_30d), sub: `${fmt.int(a.active_users_30d)} of ${fmt.int(a.total_seats)} enabled seats` }),
    a.zero_use_7d_pct == null
      ? tile({ label: 'Zero-use in first 7 days', gap: 'needs enablement roster' })
      : tile({ label: 'Zero-use in first 7 days', value: fmt.pct(a.zero_use_7d_pct), sub: `of ${a.zero_use_7d_cohort} accounts enabled ≥7d` }),
    a.zero_use_30d_pct == null
      ? tile({ label: 'Zero-use in first 30 days', gap: 'needs enablement roster' })
      : tile({ label: 'Zero-use in first 30 days', value: fmt.pct(a.zero_use_30d_pct), sub: `of ${a.zero_use_30d_cohort} accounts enabled ≥30d` }),
    tile({ label: 'Enabled accounts', value: fmt.int(a.enabled_accounts), sub: `${fmt.int(a.total_seats)} total seats` }),
  ].join('');
  const s = m.adoption.series || {};
  if (s.active_pct_of_seats_30d) {
    lineChartCard('adoption-charts', { title: 'Active users as % of enabled seats (trailing 30d)', series: s.active_pct_of_seats_30d, isPct: true });
  }
  if (s.active_users) {
    lineChartCard('adoption-charts', { title: 'Daily active users (any AI feature)', series: s.active_users });
  }
}

function renderCopilot(m) {
  const c = m.copilot.current;
  document.getElementById('copilot-tiles').innerHTML = [
    tile({ label: 'Sessions (7d)', value: fmt.int(c.sessions_7d), sub: deltaBadge(c.sessions_7d, c.sessions_prev_7d) }),
    tile({ label: 'Queries per session', value: fmt.num(c.queries_per_session_7d) }),
    tile({ label: 'Tool calls (7d)', value: fmt.int(c.tool_calls_7d), sub: deltaBadge(c.tool_calls_7d, c.tool_calls_prev_7d) }),
    c.sessions_per_shipment_7d == null
      ? tile({ label: 'Sessions per shipment', gap: 'needs shipment_id on traces' })
      : tile({ label: 'Sessions per shipment', value: fmt.num(c.sessions_per_shipment_7d) }),
    c.fallback_rate_7d == null
      ? tile({ label: 'Fallback rate', gap: 'see instrumentation gaps' })
      : tile({ label: 'Fallback rate', value: fmt.pct(c.fallback_rate_7d),
               sub: deltaBadge(c.fallback_rate_7d, c.fallback_rate_prev_7d, { goodWhenUp: false, isPct: true }) }),
    tile({ label: 'Active users (7d)', value: fmt.int(c.active_users_7d) }),
  ].join('');
  const s = m.copilot.series || {};
  if (s.sessions) lineChartCard('copilot-charts', { title: 'Copilot sessions per day', series: s.sessions });
  if (s.queries_per_session) lineChartCard('copilot-charts', { title: 'Queries per session', series: s.queries_per_session, valueFmt: v => fmt.num(v) });
  if ((m.copilot.by_page || []).length) {
    barChartCard('copilot-charts', {
      title: 'Sessions by page (7d)',
      rows: m.copilot.by_page.map(p => ({ label: p.page, value: p.sessions_7d, share: p.share })),
    });
  }
}

function renderMcp(m) {
  const x = m.mcp.current;
  document.getElementById('mcp-tiles').innerHTML = [
    tile({ label: 'Tool calls (7d)', value: fmt.int(x.calls_7d), sub: deltaBadge(x.calls_7d, x.calls_prev_7d) }),
    tile({ label: 'Error rate (7d)', value: fmt.pct(x.error_rate_7d),
           sub: deltaBadge(x.error_rate_7d, x.error_rate_prev_7d, { goodWhenUp: false, isPct: true }) }),
    tile({ label: 'Active accounts (7d)', value: fmt.int(x.active_accounts_7d) }),
    x.calls_per_connection_7d == null
      ? tile({ label: 'Calls per connection', gap: 'needs session counts' })
      : tile({ label: 'Calls per connection (7d)', value: fmt.num(x.calls_per_connection_7d) }),
    tile({ label: 'Distinct tools per account', value: fmt.num(x.avg_distinct_tools_per_account_7d), sub: 'avg, last 7d' }),
  ].join('');
  const s = m.mcp.series || {};
  if (s.calls) lineChartCard('mcp-charts', { title: 'MCP tool calls per day', series: s.calls });
  if (s.error_rate) lineChartCard('mcp-charts', { title: 'MCP error rate per day', series: s.error_rate, color: RATE, isPct: true });

  const tbody = document.querySelector('#tool-table tbody');
  tbody.innerHTML = '';
  for (const t of m.mcp.tools || []) {
    const errCls = t.error_rate_7d > 0.10 ? 'err-bad' : t.error_rate_7d > 0.05 ? 'err-warn' : 'err-ok';
    const o = t.outcomes_7d || {};
    const errTip = `user_error ${o.user_error || 0} · upstream_error ${o.upstream_error || 0} · rate_limited ${o.rate_limited || 0}`;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td class="tool-name">${t.tool}</td>
      <td class="num">${fmt.int(t.calls_7d)}</td>
      <td class="num"><span class="err-pill ${errCls}" title="${errTip}">${fmt.pct(t.error_rate_7d)}</span></td>
      <td class="num">${fmt.ms(t.p50_ms)}</td>
      <td class="num">${fmt.ms(t.p95_ms)}</td>
      <td class="spark-cell"><canvas width="120" height="28"></canvas></td>`;
    tbody.appendChild(tr);
    sparkline(tr.querySelector('canvas'), t.spark || []);
  }
}

function renderAccessorial(m) {
  const a = m.accessorial.current;
  document.getElementById('accessorial-tiles').innerHTML = [
    tile({ label: 'Recommendations (7d)', value: fmt.int(a.recommendations_7d) }),
    a.recs_per_ltl_7d == null
      ? tile({ label: 'Recs per LTL shipment', gap: 'LTL denominator missing' })
      : tile({ label: 'Recs per LTL shipment', value: fmt.pct(a.recs_per_ltl_7d), sub: `${fmt.int(a.ltl_shipments_7d)} LTL shipments` }),
    tile({ label: 'Acceptance rate', value: fmt.pct(a.acceptance_rate_7d), sub: 'accepted / recommended' }),
    tile({ label: 'Validator click-through', value: fmt.pct(a.validator_click_rate_7d), sub: 'clicked into validator / recs' }),
  ].join('');
  const s = m.accessorial.series || {};
  if (s.recommendations) lineChartCard('accessorial-charts', { title: 'Accessorial recommendations per day', series: s.recommendations });
  if (s.acceptance_rate) lineChartCard('accessorial-charts', { title: 'Acceptance rate per day', series: s.acceptance_rate, color: RATE, isPct: true });
}

function renderGaps(m) {
  if (!(m.gaps || []).length) return;
  document.getElementById('gaps-section').hidden = false;
  document.getElementById('gap-cards').innerHTML = m.gaps.map(g => `
    <div class="gap-card">
      <div class="metric">🔧 ${g.metric}</div>
      <div class="reason">${g.reason}</div>
      <div class="needs">Needs: <code>${g.needs}</code></div>
    </div>`).join('');
}

/* ---------- boot ---------- */

fetch('metrics.json')
  .then(r => { if (!r.ok) throw new Error(`metrics.json ${r.status}`); return r.json(); })
  .then(m => {
    document.getElementById('data-through').textContent = `Data through ${m.data_through} (UTC)`;
    renderSourcePills(m.source_status || {});
    const anyLive = Object.values(m.source_status || {}).some(s => s.mode === 'live');
    document.getElementById('mock-banner').hidden = anyLive;
    renderAdoption(m);
    renderCopilot(m);
    renderMcp(m);
    renderAccessorial(m);
    renderGaps(m);
    document.getElementById('page-foot').textContent =
      `Generated ${m.generated_at} · KPI window: last ${m.window_days} full UTC days · refreshed daily by GitHub Actions`;
  })
  .catch(err => {
    document.querySelector('.page').insertAdjacentHTML('afterbegin',
      `<div class="mock-banner">Failed to load metrics.json — ${err.message}. Run the pipeline first (see README).</div>`);
  });
