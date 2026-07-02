/* CDS Pipeline Dashboard — app.js */

// ── shared state ──────────────────────────────────────────────────────────────
const charts = {};

function dateParams() {
  const f = document.getElementById('from-date')?.value;
  const t = document.getElementById('to-date')?.value;
  const p = new URLSearchParams();
  if (f) p.set('from', f);
  if (t) p.set('to', t);
  return p.toString() ? '?' + p.toString() : '';
}

function clearDates() {
  const f = document.getElementById('from-date');
  const t = document.getElementById('to-date');
  if (f) f.value = '';
  if (t) t.value = '';
  loadAll();
}

async function apiFetch(url) {
  const r = await fetch(url);
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || 'API error');
  return j.data;
}

function usd(v) { return '$' + Number(v).toFixed(4); }
function usdShort(v) {
  const n = Number(v);
  return n >= 1 ? '$' + n.toFixed(2) : '$' + n.toFixed(4);
}
function pct(v) { return Number(v).toFixed(1) + '%'; }
function stars(r) { return '⭐'.repeat(Number(r)) || '—'; }
function ratingBadge(r) {
  return `<span class="rating-badge rating-${r}">${r}</span>`;
}
function fmt(v, decimals = 0) {
  if (v === null || v === undefined) return '—';
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: decimals });
}

function destroyChart(id) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
}

// ── DASHBOARD PAGE ────────────────────────────────────────────────────────────
async function loadSummary() {
  try {
    const d = await apiFetch('/api/metrics/summary' + dateParams());
    document.getElementById('kpi-sent').textContent    = fmt(d.alerts_sent);
    document.getElementById('kpi-rated').textContent   = fmt(d.alerts_rated);
    document.getElementById('kpi-response').textContent = pct(d.response_rate);
    document.getElementById('kpi-mean').textContent    = d.mean_rating ? d.mean_rating.toFixed(2) + ' / 5' : '—';
    document.getElementById('kpi-raters').textContent  = d.rater_count + ' rater(s)';

    // Rating distribution chart
    destroyChart('dist');
    const ctx = document.getElementById('chart-dist').getContext('2d');
    const dist = d.distribution;
    charts['dist'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: ['1 ★', '2 ★', '3 ★', '4 ★', '5 ★'],
        datasets: [{
          label: 'Ratings',
          data: [dist['1'], dist['2'], dist['3'], dist['4'], dist['5']],
          backgroundColor: ['#dc3545','#fd7e14','#ffc107','#20c997','#198754'],
          borderRadius: 4,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { stepSize: 1 } } }
      }
    });
  } catch (e) { console.error('loadSummary', e); }
}

async function loadTimeseries() {
  try {
    const bucket = document.getElementById('ts-bucket-inline')?.value || 'day';
    const d = await apiFetch('/api/metrics/timeseries?bucket=' + bucket + (dateParams() ? '&' + dateParams().slice(1) : ''));
    destroyChart('ts');
    const ctx = document.getElementById('chart-timeseries').getContext('2d');
    const labels = d.map(r => r.period.slice(0, 10));
    charts['ts'] = new Chart(ctx, {
      data: {
        labels,
        datasets: [
          {
            type: 'bar', label: 'Alerts Sent',
            data: d.map(r => r.alerts_sent),
            backgroundColor: 'rgba(13,110,253,0.15)',
            borderColor: 'rgba(13,110,253,0.4)',
            borderWidth: 1, yAxisID: 'yVol', order: 2,
          },
          {
            type: 'line', label: 'Avg Rating',
            data: d.map(r => r.mean_rating || null),
            borderColor: '#198754', backgroundColor: 'rgba(25,135,84,0.1)',
            tension: 0.3, fill: true, yAxisID: 'yRating', order: 1,
            pointRadius: 4,
          },
        ]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom', labels: { font: { size: 11 } } } },
        scales: {
          yVol:    { type: 'linear', position: 'right', beginAtZero: true, title: { display: true, text: 'Alerts', font: { size: 10 } }, grid: { drawOnChartArea: false } },
          yRating: { type: 'linear', position: 'left',  min: 1, max: 5, title: { display: true, text: 'Avg Rating', font: { size: 10 } } },
        }
      }
    });
  } catch (e) { console.error('loadTimeseries', e); }
}

async function loadByProblem() {
  try {
    const d = await apiFetch('/api/metrics/by-problem' + dateParams());
    destroyChart('prob');
    const ctx = document.getElementById('chart-problem').getContext('2d');
    const labels = d.map(r => r.problem_name || 'unknown');
    const ratings = d.map(r => r.mean_rating);
    const colors  = ratings.map(r => r === null ? '#adb5bd' : r >= 4 ? '#198754' : r >= 3 ? '#20c997' : r >= 2 ? '#fd7e14' : '#dc3545');
    charts['prob'] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'Avg Rating',
          data: ratings,
          backgroundColor: colors,
          borderRadius: 3,
        }]
      },
      options: {
        indexAxis: 'y',
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { min: 0, max: 5 } }
      }
    });
  } catch (e) { console.error('loadByProblem', e); }
}

async function loadCost() {
  try {
    const d = await apiFetch('/api/metrics/cost' + dateParams());
    document.getElementById('kpi-total-cost').textContent    = usdShort(d.total_cost_usd);
    document.getElementById('kpi-run-count').textContent     = d.run_count + ' runs';
    document.getElementById('kpi-per-run').textContent       = usdShort(d.avg_cost_per_run);
    document.getElementById('kpi-per-patient').textContent   = usdShort(d.avg_cost_per_patient);
    document.getElementById('kpi-total-patients').textContent = fmt(d.total_patients) + ' patients processed';

    // Costliest step — find max by value, don't trust key order (Flask may sort keys)
    const steps = Object.entries(d.by_step || {});
    const topStep = steps.length
      ? steps.reduce((best, cur) => cur[1] > best[1] ? cur : best)
      : null;
    document.getElementById('kpi-top-step').textContent =
      topStep ? topStep[0].replace(/_/g, ' ') : '—';

    // Cost-by-step table (sort descending by cost for display)
    const stepsSorted = steps.slice().sort((a, b) => b[1] - a[1]);
    const totalStepCost = stepsSorted.reduce((s, [,v]) => s + v, 0);
    const tbody = document.querySelector('#tbl-steps tbody');
    tbody.innerHTML = stepsSorted.length
      ? stepsSorted.map(([step, cost]) => `
          <tr>
            <td>${step.replace(/_/g,' ')}</td>
            <td class="text-end">${usd(cost)}</td>
            <td class="text-end text-muted">${totalStepCost ? pct(cost/totalStepCost*100) : '—'}</td>
          </tr>`).join('')
      : '<tr><td colspan="3" class="text-muted text-center py-3">No cost data</td></tr>';
  } catch (e) { console.error('loadCost', e); }
}

async function loadAlertsPerRun() {
  try {
    const d = await apiFetch('/api/metrics/alerts-per-run' + dateParams());
    document.getElementById('kpi-avg-alerts').textContent = d.avg_alerts_per_run !== undefined
      ? fmt(d.avg_alerts_per_run, 1) : '—';
    document.getElementById('kpi-run-count-alerts').textContent =
      d.run_count ? d.run_count + ' run(s)' : '';

    destroyChart('alertsPerRun');
    const ctx = document.getElementById('chart-alerts-per-run').getContext('2d');
    const ts = d.timeseries || [];
    const pointR = ts.length > 20 ? 2 : 4;
    charts['alertsPerRun'] = new Chart(ctx, {
      type: 'line',
      data: {
        labels: ts.map(r => r.run_started_at.slice(0, 16).replace('T', ' ')),
        datasets: [{
          label: 'Alerts sent',
          data: ts.map(r => r.alerts_sent),
          borderColor: '#0d6efd',
          backgroundColor: 'rgba(13,110,253,0.08)',
          fill: true, tension: 0.2, pointRadius: pointR,
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: true, ticks: { stepSize: 1, precision: 0 } },
          x: { ticks: { maxTicksLimit: 12, maxRotation: 30 } }
        }
      }
    });
  } catch (e) { console.error('loadAlertsPerRun', e); }
}

async function loadRaters() {
  try {
    const d = await apiFetch('/api/metrics/by-rater' + dateParams());
    const tbody = document.querySelector('#tbl-raters tbody');
    tbody.innerHTML = d.length
      ? d.map(r => `
          <tr>
            <td>${r.user_display || r.user_email}</td>
            <td class="text-center">${r.ratings_count}</td>
            <td class="text-center fw-semibold">${r.mean_rating.toFixed(2)}</td>
            ${[5,4,3,2,1].map(i => `<td class="text-center text-muted">${r.distribution[i] || 0}</td>`).join('')}
          </tr>`).join('')
      : '<tr><td colspan="8" class="text-muted text-center py-3">No ratings yet</td></tr>';
  } catch (e) { console.error('loadRaters', e); }
}

async function loadAgreement() {
  try {
    const d = await apiFetch('/api/metrics/agreement' + dateParams());
    const el = document.getElementById('agreement-body');
    if (!d.sufficient_data) {
      el.innerHTML = `<div class="text-muted small p-2"><i class="bi bi-info-circle me-1"></i>${d.message}</div>`;
      return;
    }
    const metrics = [
      { label: 'Krippendorff\'s α (ordinal)', value: d.krippendorff_alpha !== null ? d.krippendorff_alpha.toFixed(3) : '—', note: d.interpretation },
      { label: '% Exact Agreement', value: d.pct_exact_agreement !== null ? pct(d.pct_exact_agreement) : '—' },
      { label: '% Within-1 Agreement', value: d.pct_within1_agreement !== null ? pct(d.pct_within1_agreement) : '—' },
      { label: 'Multi-rater alerts (N)', value: d.multi_rater_alerts },
      { label: 'Total ratings', value: d.total_ratings },
    ];
    el.innerHTML = `
      <div class="mb-2 small">
        ${metrics.map(m => `
          <div class="agreement-metric">
            <span class="agreement-label">${m.label}</span>
            <span class="agreement-value">${m.value}${m.note ? ` <span class="text-muted fw-normal" style="font-size:0.75rem">(${m.note})</span>` : ''}</span>
          </div>`).join('')}
      </div>`;
  } catch (e) { console.error('loadAgreement', e); }
}

async function loadRuns() {
  const tbody = document.querySelector('#tbl-runs tbody');
  try {
    const rows = await apiFetch('/api/metrics/runs' + dateParams());
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="9" class="text-muted text-center py-3">No runs in range</td></tr>';
      return;
    }
    const hasTiers = rows.some(r => r.expensive_count !== null);
    tbody.innerHTML = rows.map(r => {
      const ts = r.run_started_at.slice(0, 16).replace('T', ' ');
      const scheduled  = hasTiers && r.total_scheduled   != null ? fmt(r.total_scheduled)   : '—';
      const skipped    = hasTiers && r.skipped_count      != null ? fmt(r.skipped_count)     : '—';
      const cheap      = hasTiers && r.cheap_count        != null ? fmt(r.cheap_count)       : '—';
      const expensive  = hasTiers && r.expensive_count    != null ? fmt(r.expensive_count)   : '—';
      const avgCheap   = hasTiers && r.avg_cost_cheap_usd    != null ? usd(r.avg_cost_cheap_usd)    : '—';
      const avgExp     = hasTiers && r.avg_cost_expensive_usd != null ? usd(r.avg_cost_expensive_usd) : '—';
      const repCharts = r.report_charts != null ? fmt(r.report_charts) : '—';
      const repCost   = r.report_cost_usd != null ? usdShort(r.report_cost_usd) : '—';
      return `<tr>
        <td class="text-muted">${ts}</td>
        <td class="text-end">${scheduled}</td>
        <td class="text-end">${skipped}</td>
        <td class="text-end">${cheap}</td>
        <td class="text-end">${expensive}</td>
        <td class="text-end">${fmt(r.alerts_sent)}</td>
        <td class="text-end fw-semibold">${usdShort(r.cost_usd)}</td>
        <td class="text-end text-muted">${avgCheap}</td>
        <td class="text-end text-muted">${avgExp}</td>
        <td class="text-end text-muted">${repCharts}</td>
        <td class="text-end text-muted">${repCost}</td>
      </tr>`;
    }).join('');

    // Populate the run audit picker
    const picker = document.getElementById('audit-run-picker');
    if (picker) {
      const current = picker.value;
      picker.innerHTML = '<option value="">— select a run —</option>' +
        rows.map(r => {
          const ts = r.run_started_at.slice(0, 16).replace('T', ' ');
          const label = `${ts}  (${fmt(r.expensive_count || 0)} exp, ${fmt(r.alerts_sent)} alerts)`;
          return `<option value="${r.run_started_at}">${escHtml(label)}</option>`;
        }).join('');
      if (current) picker.value = current;
    }
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-danger text-center py-3">Error loading runs</td></tr>';
    console.error('loadRuns', e);
  }
}

// ── suppression rule badge ────────────────────────────────────────────────────
function suppressionBadge(rule) {
  if (!rule || rule === 'none') return '<span class="badge bg-secondary bg-opacity-25 text-secondary">—</span>';
  const r = String(rule);
  let bg, label;
  if (r === 'alerted')                  { bg = '#dc3545'; label = 'alerted'; }
  else if (r.startsWith('permissive'))  { bg = '#fd7e14'; label = r.replace('permissive_window:', 'protocol:'); }
  else if (r === 'being_addressed')     { bg = '#6c757d'; label = 'being addressed'; }
  else if (r === 'model_suppressed')    { bg = '#6c757d'; label = 'model suppressed'; }
  else if (r.startsWith('below_floor')) { bg = '#0d6efd'; label = r.replace('below_floor:', 'below floor: '); }
  else if (r === 'cooldown')            { bg = '#198754'; label = 'cooldown'; }
  else if (r.startsWith('stale'))       { bg = '#6610f2'; label = r.replace('_', ' '); }
  else if (r.startsWith('improving'))   { bg = '#20c997'; label = r.replace('improving_trend:', '↑ '); }
  else if (r.startsWith('gcs'))         { bg = '#ffc107'; label = r.replace('gcs_stability:', 'GCS '); }
  else if (r.startsWith('lab_value'))   { bg = '#212529'; label = r.replace('lab_value_mismatch:', 'mismatch: '); }
  else                                  { bg = '#adb5bd'; label = r; }
  return `<span class="badge" style="background:${bg};font-size:0.7rem">${escHtml(label)}</span>`;
}

async function loadRunAudit(runTs) {
  const tbody = document.querySelector('#tbl-audit tbody');
  if (!runTs) {
    tbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center py-3">Select a run above to load audit data</td></tr>';
    return;
  }
  tbody.innerHTML = '<tr><td colspan="5" class="text-center py-3"><div class="spinner-border spinner-border-sm text-primary"></div></td></tr>';
  try {
    const rows = await apiFetch('/api/metrics/run-audit?run=' + encodeURIComponent(runTs));
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center py-3">No audit data for this run — data is written starting from the next run after deploy.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(r => {
      const pass1Label = r.pass1_tag === 'upstream_gate'
        ? '<span class="badge bg-info bg-opacity-75 text-dark">lab/vital gate</span>'
        : r.pass1_needs_full
          ? `<span class="badge bg-danger bg-opacity-75">${escHtml(r.pass1_tag || 'flagged')}</span>`
          : `<span class="badge bg-secondary bg-opacity-25 text-secondary">cheap</span>`;

      const pass2Label = (() => {
        const p = r.pass2_outcome || '';
        if (p === 'skipped_by_pass1')            return '<span class="badge bg-secondary bg-opacity-20 text-muted">skipped</span>';
        if (p === 'forced_by_overdue_next_check') return '<span class="badge bg-warning text-dark">forced (overdue)</span>';
        if (p === 'upstream_forced')             return '<span class="badge bg-danger bg-opacity-75">expensive (gate forced)</span>';
        if (p.startsWith('pass2:'))              return `<span class="badge bg-primary bg-opacity-75">${escHtml(p.replace('pass2:',''))}</span>`;
        if (p)                                   return `<span class="badge bg-secondary">${escHtml(p)}</span>`;
        return '—';
      })();

      const whyExpensive = (() => {
        const scheduling = r.trigger_reason || '';
        const screener   = r.pass1_needs_full ? (r.pass1_tag || '') : '';
        if (!scheduling && !screener) return '<span class="text-muted small">—</span>';
        const parts = [];
        if (scheduling) parts.push(`<div style="font-size:0.75rem;margin-bottom:2px"><span class="text-muted fw-semibold">Scheduling: </span><span class="text-muted">${escHtml(scheduling)}</span></div>`);
        if (screener)   parts.push(`<div style="font-size:0.75rem"><span class="text-muted fw-semibold">Screener: </span><span class="text-muted">${escHtml(screener)}</span></div>`);
        return parts.join('');
      })();

      const problems = (r.problems || []);
      const problemsHtml = problems.length
        ? `<div class="d-flex flex-column gap-1">` +
          problems.map(p => {
            const reasoning = p.tracker_reasoning || p.alert_reason || '';
            const truncated = reasoning.length > 120 ? reasoning.slice(0, 120) + '…' : reasoning;
            return `<div style="font-size:0.78rem">
              <strong>${escHtml(p.problem_name || '')}</strong>
              <span class="text-muted ms-1">[${escHtml(p.clinical_status || '?')}]</span>
              ${suppressionBadge(p.suppression_rule)}
              ${truncated ? `<div class="text-muted mt-1" style="font-size:0.72rem;line-height:1.3">${escHtml(truncated)}</div>` : ''}
            </div>`;
          }).join('') +
          `</div>`
        : '<span class="text-muted small">—</span>';

      const outcomeBadge = (() => {
        const o = r.pipeline_outcome || '';
        if (o === 'alerted')             return '<span class="badge bg-danger">alerted</span>';
        if (o === 'expensive_no_alert')  return '<span class="badge bg-warning text-dark">expensive, no alert</span>';
        if (o === 'cheap')               return '<span class="badge bg-secondary bg-opacity-25 text-secondary">cheap</span>';
        if (o.startsWith('error'))       return `<span class="badge bg-dark">${escHtml(o)}</span>`;
        return `<span class="badge bg-secondary">${escHtml(o)}</span>`;
      })();

      return `<tr>
        <td class="align-top" style="white-space:nowrap">
          <span class="fw-semibold" style="font-size:0.82rem">${escHtml(r.CPMRN)}</span><br>
          ${outcomeBadge}
        </td>
        <td class="align-top">${pass1Label}</td>
        <td class="align-top">${whyExpensive}</td>
        <td class="align-top">${pass2Label}</td>
        <td class="align-top">${problemsHtml}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-danger text-center py-3">Error: ${escHtml(String(e))}</td></tr>`;
    console.error('loadRunAudit', e);
  }
}

async function loadComments() {
  try {
    const d = await apiFetch('/api/feedback/comments' + dateParams());
    const tbody = document.querySelector('#tbl-comments tbody');
    tbody.innerHTML = d.length
      ? d.map(r => `
          <tr>
            <td>${r.problem_name || '—'}</td>
            <td class="text-center">${ratingBadge(r.rating)}</td>
            <td>${escHtml(r.comment)}</td>
            <td class="text-muted">${r.user}</td>
            <td class="text-muted">${r.created_at.slice(0,16).replace('T',' ')}</td>
          </tr>`).join('')
      : '<tr><td colspan="5" class="text-muted text-center py-3">No comments yet</td></tr>';
  } catch (e) { console.error('loadComments', e); }
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

async function loadAll() {
  if (window.PAGE !== 'dashboard') return;
  const ts = new Date().toLocaleTimeString();
  const el = document.getElementById('last-updated');
  if (el) el.textContent = 'Loading…';
  await Promise.all([
    loadSummary(), loadTimeseries(), loadAlertsPerRun(),
    loadCost(), loadRaters(), loadAgreement(), loadComments(),
  ]);
  if (el) el.textContent = 'Updated ' + ts;
}

// ── CONFIG PAGE ───────────────────────────────────────────────────────────────
function showTab(name, linkEl) {
  ['protocols','lab','symptom','staleness','ops'].forEach(t => {
    document.getElementById('tab-' + t)?.classList.toggle('d-none', t !== name);
  });
  document.querySelectorAll('#config-tabs .nav-link').forEach(l => l.classList.remove('active'));
  if (linkEl) linkEl.classList.add('active');
}

async function loadConfig() {
  if (window.PAGE !== 'config') return;
  await Promise.all([
    loadProtocols(), loadLabRules(), loadLabNormalRanges(), loadSymptomRules(), loadLabStaleness(), loadOpsSettings()
  ]);
}

async function loadProtocols() {
  const el = document.getElementById('protocols-content');
  try {
    const protocols = await apiFetch('/api/config/protocols');
    if (!protocols.length) {
      el.innerHTML = '<div class="text-muted small">No protocols found in GCS or seed script.</div>';
      return;
    }
    el.innerHTML = protocols.map(p => `
      <div class="protocol-card mb-3">
        <div class="protocol-header d-flex align-items-center gap-2">
          <i class="bi bi-signpost-split text-primary"></i>
          ${escHtml(p.protocol_id)}
          <span class="badge bg-primary bg-opacity-10 text-primary source-badge ms-1">
            ${p._source === 'seed_script' ? 'seed script (not yet in GCS)' : 'GCS'}
          </span>
          <button class="btn btn-xs btn-outline-primary ms-auto py-0 px-2"
            style="font-size:0.75rem"
            onclick="openEditProtocol(this.dataset.p)"
            data-p="${escHtml(JSON.stringify(p)).replace(/"/g,'&quot;')}">
            <i class="bi bi-pencil me-1"></i>Edit
          </button>
        </div>
        <div class="protocol-body">
          <p class="text-muted small mb-2"><strong>Applies when:</strong> ${(p.applies_when || []).map(escHtml).join(', ')}</p>
          <p class="small mb-3"><strong>Gate question:</strong> ${escHtml(p.gate_question || '')}</p>

          ${p.scenarios?.length ? `
          <div class="table-responsive mb-3">
            <table class="table table-sm scenario-table">
              <thead><tr>
                <th>Scenario</th><th>Permitted Band</th><th>Window</th><th>Invalidate if</th>
              </tr></thead>
              <tbody>
                ${p.scenarios.map(s => `
                  <tr>
                    <td><strong>${escHtml(s.name||'')}</strong><br>
                        <span class="text-muted">${escHtml(s.description||'')}</span></td>
                    <td><code>${escHtml(s.band_description||'')}</code></td>
                    <td>${escHtml(s.window||'')}</td>
                    <td><ul class="invalidate-list">
                      ${(s.invalidate_if||[]).map(i => `<li>${escHtml(i)}</li>`).join('')}
                    </ul></td>
                  </tr>`).join('')}
              </tbody>
            </table>
          </div>` : ''}

          ${p.escalation_target_after_window ? `
          <div class="alert alert-info py-2 px-3 small mb-2">
            <strong>After window:</strong> ${escHtml(p.escalation_target_after_window)}
          </div>` : ''}

          ${p.guidance ? `
          <div class="mb-2">
            <strong class="small text-success"><i class="bi bi-lightbulb me-1"></i>Guidance (injected into system prompt)</strong>
            <pre class="guidance-block mt-1 mb-0">${escHtml(p.guidance)}</pre>
          </div>` : ''}

          ${p.audit ? `
          <div class="alert alert-warning py-2 px-3 small mb-2">
            <strong><i class="bi bi-clipboard-check me-1"></i>Documentation Audit</strong> —
            triggered <strong>${escHtml(String(p.audit.window_hours || 12))}h</strong> after first detection
            <ul class="mb-0 mt-1">
              ${(p.audit.required_documentation || []).map(item => `<li>${escHtml(item)}</li>`).join('')}
            </ul>
          </div>` : ''}

          <span class="raw-json-toggle" onclick="toggleRaw(this)">
            <i class="bi bi-code me-1"></i>Show raw JSON
          </span>
          <div class="raw-json-block d-none mt-1">${escHtml(JSON.stringify(p, null, 2))}</div>
        </div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = `<div class="text-danger small">Error: ${escHtml(String(e))}</div>`;
  }
}

async function loadLabRules() {
  const el = document.getElementById('lab-content');
  try {
    const d = await apiFetch('/api/config/lab-rules');
    const rules = d.rules || [];
    const enabled = d.enabled !== false;
    el.innerHTML = `
      <div class="mb-2 small">
        <span class="enabled-dot enabled-dot--${enabled?'on':'off'}"></span>
        <strong>${enabled ? 'Enabled' : 'Disabled'}</strong> — ${rules.length} rule(s)
      </div>
      ${rules.length ? `
      <div class="table-responsive">
        <table class="table table-sm table-hover">
          <thead><tr>
            <th>Lab</th><th>Floor</th><th>Ceiling</th>
            <th>Delta</th><th>Logic</th><th>Notes</th>
          </tr></thead>
          <tbody>
            ${rules.map(r => `
              <tr>
                <td><strong>${escHtml(r.lab||'')}</strong>
                  <div class="text-muted" style="font-size:0.72rem">${(r.aliases||[]).join(', ')}</div>
                </td>
                <td>${r.absolute_floor != null ? `<code>&lt; ${r.absolute_floor.toLocaleString()} ${r.unit||''}</code>` : '—'}</td>
                <td>${r.absolute_ceiling != null ? `<code>&gt; ${r.absolute_ceiling.toLocaleString()} ${r.unit||''}</code>` : '—'}</td>
                <td>${r.delta_pct != null ? `${r.delta_direction||'change'} ≥${r.delta_pct}%` :
                     r.delta_abs != null ? `${r.delta_direction||'change'} &gt;${r.delta_abs} ${r.unit||''}` : '—'}</td>
                <td><span class="badge bg-secondary bg-opacity-25 text-dark">${r.logic||''}</span></td>
                <td class="text-muted" style="font-size:0.78rem">${escHtml(r.notes||'')}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>` : '<div class="text-muted small">No rules defined.</div>'}
      <span class="raw-json-toggle" onclick="toggleRaw(this)">
        <i class="bi bi-code me-1"></i>Show raw JSON
      </span>
      <div class="raw-json-block d-none mt-1">${escHtml(JSON.stringify(d, null, 2))}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="text-danger small">Error: ${escHtml(String(e))}</div>`;
  }
}

async function loadSymptomRules() {
  const el = document.getElementById('symptom-content');
  try {
    const d = await apiFetch('/api/config/symptom-rules');
    const rules = d.rules || [];
    const enabled = d.enabled !== false;
    const fromSeed = d._source === 'seed_script';
    el.innerHTML = `
      <div class="mb-2 small">
        <span class="enabled-dot enabled-dot--${enabled?'on':'off'}"></span>
        <strong>${enabled ? 'Enabled' : 'Disabled'}</strong> — ${rules.length} rule(s)
        ${fromSeed ? '<span class="badge bg-warning text-dark source-badge ms-2">seed script defaults</span>' : ''}
      </div>
      ${rules.map(r => `
        <div class="card mb-2">
          <div class="card-body py-2 px-3">
            <div class="d-flex align-items-start gap-2">
              <strong style="min-width:90px">${escHtml(r.problem||'')}</strong>
              <div class="text-muted small">${(r.aliases||[]).map(escHtml).join(', ')}</div>
              ${r.min_score != null ? `<span class="badge bg-info text-dark ms-auto">${r.score_name}: ≥${r.min_score}</span>` : ''}
            </div>
            <div class="mt-1 small"><strong>Must have one of:</strong>
              <ul class="mb-0 mt-1">
                ${(r.objective_criteria||[]).map(c => `<li>${escHtml(c)}</li>`).join('')}
              </ul>
            </div>
            ${r.notes ? `<div class="text-muted mt-1" style="font-size:0.78rem"><i class="bi bi-info-circle me-1"></i>${escHtml(r.notes)}</div>` : ''}
          </div>
        </div>`).join('')}
      <span class="raw-json-toggle mt-2 d-inline-block" onclick="toggleRaw(this)">
        <i class="bi bi-code me-1"></i>Show raw JSON
      </span>
      <div class="raw-json-block d-none mt-1">${escHtml(JSON.stringify(d, null, 2))}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="text-danger small">Error: ${escHtml(String(e))}</div>`;
  }
}

async function loadLabStaleness() {
  const el = document.getElementById('staleness-content');
  try {
    const d = await apiFetch('/api/config/lab-staleness-overrides');
    const overrides = d.overrides || {};
    const enabled = d.enabled !== false;
    const fromSeed = d._source === 'seed_script';
    const entries = Object.entries(overrides);
    el.innerHTML = `
      <div class="mb-2 small">
        <span class="enabled-dot enabled-dot--${enabled?'on':'off'}"></span>
        <strong>${enabled ? 'Enabled' : 'Disabled'}</strong> — ${entries.length} override(s)
        ${fromSeed ? '<span class="badge bg-warning text-dark source-badge ms-2">seed script defaults</span>' : ''}
      </div>
      ${entries.length ? `
      <div class="table-responsive">
        <table class="table table-sm table-hover" style="max-width:480px">
          <thead><tr>
            <th>Lab</th><th>Max age</th><th class="text-muted">Global default</th>
          </tr></thead>
          <tbody>
            ${entries.map(([lab, hours]) => `
              <tr>
                <td><strong>${escHtml(lab)}</strong></td>
                <td><code>${hours}h</code></td>
                <td class="text-muted">24h</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>` : '<div class="text-muted small">No overrides defined — global default of 24h applies to all labs.</div>'}
      <span class="raw-json-toggle" onclick="toggleRaw(this)">
        <i class="bi bi-code me-1"></i>Show raw JSON
      </span>
      <div class="raw-json-block d-none mt-1">${escHtml(JSON.stringify(d, null, 2))}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="text-danger small">Error: ${escHtml(String(e))}</div>`;
  }
}

async function saveStudyPipeline(enabled) {
  const card = document.getElementById('study-pipeline-card');
  const toggle = document.getElementById('study-pipeline-toggle');
  toggle.disabled = true;
  try {
    await fetch('/api/config/study-pipeline', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    card.className = `card mb-3 border-2 border-${enabled ? 'success' : 'secondary'}`;
  } catch (e) {
    console.error('saveStudyPipeline', e);
    toggle.checked = !enabled; // revert on failure
  } finally {
    toggle.disabled = false;
  }
}

async function saveMedRecon(enabled) {
  const card = document.getElementById('med-recon-card');
  const toggle = document.getElementById('med-recon-toggle');
  toggle.disabled = true;
  try {
    await fetch('/api/config/med-recon', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled}),
    });
    card.className = `card mb-3 border-2 border-${enabled ? 'success' : 'secondary'}`;
  } catch (e) {
    console.error('saveMedRecon', e);
    toggle.checked = !enabled; // revert on failure
  } finally {
    toggle.disabled = false;
  }
}

async function loadOpsSettings() {
  const el = document.getElementById('ops-content');
  try {
    const d = await apiFetch('/api/config/operational');
    // Populate study pipeline toggle
    const toggle = document.getElementById('study-pipeline-toggle');
    const card   = document.getElementById('study-pipeline-card');
    if (toggle) {
      toggle.checked = !!d.study_pipeline_enabled;
      card.className = `card mb-3 border-2 border-${d.study_pipeline_enabled ? 'success' : 'secondary'}`;
    }
    // Populate med-recon toggle
    const mrToggle = document.getElementById('med-recon-toggle');
    const mrCard   = document.getElementById('med-recon-card');
    if (mrToggle) {
      mrToggle.checked = !!d.med_recon_enabled;
      mrCard.className = `card mb-3 border-2 border-${d.med_recon_enabled ? 'success' : 'secondary'}`;
    }
    let html = '';

    function editBtn(settingId, doc) {
      const safe = escHtml(JSON.stringify(doc)).replace(/"/g,'&quot;');
      return `<button class="btn ms-auto py-0 px-2" style="font-size:0.75rem;border:1px solid #0d6efd;color:#0d6efd"
        onclick="openEditOps('${settingId}', this.dataset.d)" data-d="${safe}">
        <i class="bi bi-pencil me-1"></i>Edit</button>`;
    }

    const recipients = d.alert_recipients;
    if (recipients) {
      const emails = recipients.emails || [];
      html += `
        <div class="card mb-3">
          <div class="card-header card-header-sm d-flex align-items-center">
            <span class="enabled-dot enabled-dot--${recipients.enabled?'on':'off'}"></span>
            Alert Recipients
            ${editBtn('alert_recipients', recipients)}
          </div>
          <div class="card-body py-2">
            ${emails.map(e => `<span class="badge bg-primary bg-opacity-15 text-primary me-1 mb-1">${escHtml(e)}</span>`).join('')}
          </div>
        </div>`;
    }

    const ws = d.monitored_workspaces;
    if (ws) {
      const wsList = ws.workspaces || [];
      html += `
        <div class="card mb-3">
          <div class="card-header card-header-sm d-flex align-items-center">
            Monitored Workspaces
            ${editBtn('monitored_workspaces', ws)}
          </div>
          <div class="card-body py-2">
            ${wsList.map(w => `<span class="badge bg-success bg-opacity-15 text-success me-1">${escHtml(w)}</span>`).join('')}
          </div>
        </div>`;
    }

    const webhook = d.gchat_webhook;
    if (webhook) {
      html += `
        <div class="card mb-3">
          <div class="card-header card-header-sm d-flex align-items-center">
            <span class="enabled-dot enabled-dot--${webhook.enabled?'on':'off'}"></span>
            Google Chat Webhook
            ${editBtn('gchat_webhook', webhook)}
          </div>
          <div class="card-body py-2 small text-muted">
            <code>${escHtml(webhook.url || '')}</code>
          </div>
        </div>`;
    }

    if (!html) html = '<div class="text-muted small">No operational settings found in GCS.</div>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div class="text-danger small">Error: ${escHtml(String(e))}</div>`;
  }
}

function toggleRaw(el) {
  const block = el.nextElementSibling;
  const hidden = block.classList.toggle('d-none');
  el.innerHTML = `<i class="bi bi-code me-1"></i>${hidden ? 'Show' : 'Hide'} raw JSON`;
}

// ── CONFIG EDITING ────────────────────────────────────────────────────────────
let _editState = { endpoint: null, onSuccess: null };
let _editModal  = null;

function _modal() {
  if (!_editModal) _editModal = new bootstrap.Modal(document.getElementById('editModal'));
  return _editModal;
}

function _openEditModal(title, doc, endpoint, hint, onSuccess) {
  document.getElementById('editModalLabel').textContent = title;
  document.getElementById('edit-json').value = JSON.stringify(doc, null, 2);
  document.getElementById('edit-hint').textContent = hint || '';
  document.getElementById('edit-error').classList.add('d-none');
  _editState = { endpoint, onSuccess };
  _modal().show();
}

async function saveConfig() {
  const textarea = document.getElementById('edit-json');
  const errEl    = document.getElementById('edit-error');
  errEl.classList.add('d-none');

  let doc;
  try {
    doc = JSON.parse(textarea.value);
  } catch (e) {
    errEl.textContent = 'Invalid JSON — ' + e.message;
    errEl.classList.remove('d-none');
    return;
  }

  if (!confirm('Save to GCS? The pipeline will pick this up on the next hourly run.')) return;

  try {
    const r = await fetch(_editState.endpoint, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(doc),
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Save failed');
    _modal().hide();
    if (_editState.onSuccess) _editState.onSuccess();
  } catch (e) {
    errEl.textContent = 'Save failed: ' + (e instanceof Error ? e.message : JSON.stringify(e));
    errEl.classList.remove('d-none');
  }
}

async function openEditLabRules() {
  try {
    const d = await apiFetch('/api/config/lab-rules');
    _openEditModal('Edit Lab Alert Rules', d, '/api/config/lab-rules',
      'Saves to gs://patientview-cds-pipeline-ops/app_settings/lab_alert_rules.json',
      loadLabRules);
  } catch (e) { alert('Could not load current rules: ' + e.message); }
}

async function loadLabNormalRanges() {
  const el = document.getElementById('lab-normal-content');
  if (!el) return;
  try {
    const d = await apiFetch('/api/config/lab-normal-ranges');
    const panels = d.panels || {};
    const enabled = d.enabled !== false;
    const fromDefault = d._source === 'module_default';
    const panelKeys = Object.keys(panels);

    let rows = '';
    for (const key of panelKeys) {
      const panel = panels[key];
      const ranges = panel.ranges || [];
      if (panel.always_normal) {
        rows += `<tr>
          <td><strong>${escHtml(key)}</strong></td>
          <td colspan="3" class="text-muted fst-italic">always normal (qualitative panel)</td>
        </tr>`;
      } else {
        ranges.forEach((r, i) => {
          rows += `<tr>
            <td>${i === 0 ? `<strong>${escHtml(key)}</strong>` : ''}</td>
            <td><code>${escHtml(r.attr||'')}</code></td>
            <td>${r.low != null ? r.low : '<span class="text-muted">—</span>'}</td>
            <td>${r.high != null ? r.high : '<span class="text-muted">—</span>'}</td>
          </tr>`;
        });
        if (!ranges.length) {
          rows += `<tr>
            <td><strong>${escHtml(key)}</strong></td>
            <td colspan="3" class="text-muted">no ranges defined</td>
          </tr>`;
        }
      }
    }

    el.innerHTML = `
      <div class="mb-2 small">
        <span class="enabled-dot enabled-dot--${enabled?'on':'off'}"></span>
        <strong>${enabled ? 'Enabled' : 'Disabled'}</strong> — ${panelKeys.length} panel(s)
        ${fromDefault ? '<span class="badge bg-warning text-dark source-badge ms-2">module defaults (not yet seeded to GCS)</span>' : ''}
      </div>
      ${panelKeys.length ? `
      <div class="table-responsive">
        <table class="table table-sm table-hover">
          <thead><tr>
            <th>Panel</th><th>Analyte (attr substring)</th>
            <th>Normal low</th><th>Normal high</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>` : '<div class="text-muted small">No panels defined.</div>'}
      <span class="raw-json-toggle" onclick="toggleRaw(this)">
        <i class="bi bi-code me-1"></i>Show raw JSON
      </span>
      <div class="raw-json-block d-none mt-1">${escHtml(JSON.stringify(d, null, 2))}</div>`;
  } catch (e) {
    el.innerHTML = `<div class="text-danger small">Error: ${escHtml(String(e))}</div>`;
  }
}

async function openEditLabNormalRanges() {
  try {
    const d = await apiFetch('/api/config/lab-normal-ranges');
    _openEditModal('Edit Lab Normal Ranges', d, '/api/config/lab-normal-ranges',
      'Saves to gs://patientview-cds-pipeline-ops/app_settings/lab_normal_ranges.json',
      loadLabNormalRanges);
  } catch (e) { alert('Could not load current ranges: ' + e.message); }
}

async function openEditSymptomRules() {
  try {
    const d = await apiFetch('/api/config/symptom-rules');
    _openEditModal('Edit Symptom Alert Rules', d, '/api/config/symptom-rules',
      'Saves to gs://patientview-cds-pipeline-ops/app_settings/symptom_alert_rules.json',
      loadSymptomRules);
  } catch (e) { alert('Could not load current rules: ' + e.message); }
}

async function openEditLabStaleness() {
  try {
    const d = await apiFetch('/api/config/lab-staleness-overrides');
    _openEditModal('Edit Lab Staleness Overrides', d, '/api/config/lab-staleness-overrides',
      'Saves to gs://patientview-cds-pipeline-ops/app_settings/lab_staleness_overrides.json',
      loadLabStaleness);
  } catch (e) { alert('Could not load current overrides: ' + e.message); }
}

function openEditProtocol(protocolJson) {
  const p = JSON.parse(protocolJson);
  _openEditModal(`Edit Protocol: ${p.protocol_id}`, p, '/api/config/protocols',
    `Saves to gs://patientview-cds-pipeline-ops/monitoring_protocols/${p.protocol_id}.json`,
    loadProtocols);
}

function openEditProtocols() {
  const template = {
    protocol_id: 'new-protocol',
    applies_when: ['keyword1', 'keyword2'],
    gate_question: "Is this patient's condition currently INTENDED? Rule in or out each scenario below.",
    scenarios: [{
      name: 'scenario_name',
      description: 'Brief description',
      band_description: 'Target values',
      window: 'Duration',
      invalidate_if: ['condition 1', 'condition 2'],
    }],
    escalation_target_after_window: 'What to do once the permissive window closes.',
  };
  _openEditModal('Add New Protocol', template, '/api/config/protocols',
    'Creates a new monitoring_protocols/{protocol_id}.json in GCS',
    loadProtocols);
}

function openEditOps(settingId, docJson) {
  const labels = {
    alert_recipients:    'Edit Alert Recipients',
    gchat_webhook:       'Edit Google Chat Webhook',
    monitored_workspaces:'Edit Monitored Workspaces',
  };
  _openEditModal(
    labels[settingId] || `Edit ${settingId}`,
    JSON.parse(docJson),
    `/api/config/operational/${settingId}`,
    `Saves to gs://patientview-cds-pipeline-ops/app_settings/${settingId}.json`,
    loadOpsSettings,
  );
}

// ── DOCS PAGE ─────────────────────────────────────────────────────────────────
async function loadDocsList() {
  if (window.PAGE !== 'docs') return;
  try {
    const docs = await apiFetch('/api/docs');
    const el = document.getElementById('doc-list');
    el.innerHTML = docs.map(d => `
      <a href="#" class="doc-link" onclick="loadDoc('${d.slug}', this); return false;">
        ${escHtml(d.title)}
      </a>`).join('');
    if (docs.length) {
      const preferred = el.querySelector('[onclick*="pipeline_overview"]') || el.querySelector('.doc-link');
      if (preferred) preferred.click();
    }
  } catch (e) {
    document.getElementById('doc-list').innerHTML =
      `<div class="text-danger small">${escHtml(String(e))}</div>`;
  }
}

async function loadDoc(slug, linkEl) {
  document.querySelectorAll('.doc-link').forEach(l => l.classList.remove('active'));
  if (linkEl) linkEl.classList.add('active');
  const content = document.getElementById('doc-content');
  content.innerHTML = '<div class="spinner-wrap"><div class="spinner-border text-primary"></div></div>';
  try {
    const d = await apiFetch('/api/docs/' + slug);
    // marked.parse is sync in v12 without async extensions
    content.innerHTML = marked.parse(d.markdown);
    // Convert fenced mermaid blocks (marked renders as <pre><code class="language-mermaid">)
    content.querySelectorAll('code.language-mermaid').forEach((el) => {
      const pre = el.parentElement;
      const div = document.createElement('div');
      div.className = 'mermaid';
      div.textContent = el.textContent;
      pre.replaceWith(div);
    });
    // Render mermaid in isolation — don't crash the whole page if a diagram errors
    const mermaidNodes = content.querySelectorAll('.mermaid');
    if (mermaidNodes.length) {
      try {
        await mermaid.run({ nodes: mermaidNodes });
      } catch (mErr) {
        console.warn('mermaid render error:', mErr);
        mermaidNodes.forEach(n => {
          if (!n.querySelector('svg')) {
            n.innerHTML = `<pre class="text-warning small">[diagram render error — check console]</pre>`;
          }
        });
      }
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : (typeof e === 'object' ? JSON.stringify(e) : String(e));
    content.innerHTML = `<div class="text-danger small p-3">${escHtml(msg)}</div>`;
  }
}

// ── init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  if (window.PAGE === 'dashboard') loadAll();
  if (window.PAGE === 'config')    loadConfig();
  if (window.PAGE === 'docs')      loadDocsList();
  if (window.PAGE === 'audit')     loadAuditPage();
  if (window.PAGE === 'doc-audits') loadDocAudits();
});

// ── AUDIT PAGE ────────────────────────────────────────────────────────────────

async function loadAuditPage() {
  await Promise.all([loadAuditNextChecks(), loadAuditRuns()]);
}

async function loadDocAudits() {
  const el = document.getElementById('doc-audits-body');
  if (!el) return;
  try {
    const rows = await apiFetch('/api/audit/documentation?hours_back=72');
    if (!rows.length) {
      el.innerHTML = '<p class="text-muted text-center py-2 mb-0 small">No documentation audits in the last 72 hours.</p>';
      return;
    }
    const verdictBadge = v => {
      if (v === 'adequate_documentation') return `<span class="badge bg-success bg-opacity-75">${escHtml(v)}</span>`;
      return `<span class="badge bg-warning text-dark">${escHtml(v || '—')}</span>`;
    };
    el.innerHTML = `
      <div class="table-responsive">
        <table class="table table-sm table-hover small">
          <thead><tr>
            <th>Patient</th><th>Problem</th><th>Protocol</th>
            <th>Detected</th><th>Audited</th><th>Verdict</th>
            <th>Documented</th><th>Missing</th>
          </tr></thead>
          <tbody>
            ${rows.map(r => {
              let documented = [], missing = [];
              try { documented = JSON.parse(r.documented_items || '[]'); } catch(e) {}
              try { missing = JSON.parse(r.missing_items || '[]'); } catch(e) {}
              return `<tr>
                <td>${escHtml(r.CPMRN)} <span class="text-muted">#${r.encounter}</span></td>
                <td>${escHtml(r.problem_name)}</td>
                <td><code>${escHtml(r.protocol_id)}</code></td>
                <td class="text-muted">${escHtml(r.detected_at ? r.detected_at.replace('T',' ').slice(0,16) : '—')}</td>
                <td class="text-muted">${escHtml(r.audited_at ? r.audited_at.replace('T',' ').slice(0,16) : '—')}</td>
                <td>${verdictBadge(r.verdict)}</td>
                <td>${documented.length ? `<ul class="mb-0 ps-3">${documented.map(i=>`<li>${escHtml(i)}</li>`).join('')}</ul>` : '<span class="text-muted">—</span>'}</td>
                <td>${missing.length ? `<ul class="mb-0 ps-3 text-danger">${missing.map(i=>`<li>${escHtml(i)}</li>`).join('')}</ul>` : '<span class="text-success">none</span>'}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>`;
  } catch(e) {
    el.innerHTML = `<div class="text-danger small">Error: ${escHtml(String(e))}</div>`;
  }
}

// ── Next-checks banner ────────────────────────────────────────────────────────

async function loadAuditNextChecks() {
  const body = document.getElementById('nc-body');
  if (!body) return;
  try {
    const items = await apiFetch('/api/audit/next-checks');
    const now = Date.now();

    if (!items.length) {
      body.innerHTML = '<p class="text-muted text-center py-2 mb-0 small">No active next-checks.</p>';
      return;
    }

    const overdue  = items.filter(i => i.overdue);
    const pending  = items.filter(i => !i.overdue);

    const countBadge = document.getElementById('nc-badge-count');
    const overdueBadge = document.getElementById('nc-badge-overdue');
    if (countBadge) { countBadge.textContent = items.length + ' active'; countBadge.style.display = ''; }
    if (overdueBadge && overdue.length) { overdueBadge.textContent = overdue.length + ' overdue'; overdueBadge.style.display = ''; }

    const rows = items.map((i, idx) => {
      const due = new Date(i.due_after);
      const diffMs = due - now;
      const diffMin = Math.round(Math.abs(diffMs) / 60000);
      const fmtDuration = m => m >= 60 ? `${(m/60).toFixed(1)}h` : `${m}m`;
      const dueLabel = i.overdue
        ? `<span class="text-danger fw-semibold">${fmtDue(due)} (${fmtDuration(diffMin)} ago)</span>`
        : `<span class="text-warning">${fmtDue(due)} (in ${fmtDuration(diffMin)})</span>`;

      const statusBadge = i.overdue
        ? '<span class="badge bg-danger" style="font-size:0.7rem">overdue</span>'
        : '<span class="badge bg-warning text-dark" style="font-size:0.7rem">pending</span>';

      const watching  = escHtml(i.label || i.key || i.type || '—');
      const problem   = escHtml(i.problem_name || '—');
      const reasoning = i.tracker_reasoning
        ? `<div class="text-muted mt-1" style="font-size:0.74rem; line-height:1.35">${escHtml(i.tracker_reasoning)}</div>`
        : '';
      const detailId  = `nc-detail-${idx}`;

      const activeKey = i.key || i.type || '';
      return `<tr style="font-size:0.82rem; cursor:pointer" onclick="toggleNcDetail('${detailId}','${escHtml(i.CPMRN)}',${i.encounter},'${escHtml(i.problem_name)}','${escHtml(activeKey)}')">
        <td class="ps-3">${escHtml(i.CPMRN)}</td>
        <td><div>${problem}</div>${reasoning}</td>
        <td>${watching}</td>
        <td>${dueLabel}</td>
        <td>${statusBadge}</td>
      </tr>
      <tr id="${detailId}" style="display:none">
        <td colspan="5" style="padding:0; background:#f8f9fa; border-top:none"></td>
      </tr>`;
    }).join('');

    body.innerHTML = `
      <table class="table table-sm mb-0">
        <thead style="font-size:0.76rem; color:#6c757d">
          <tr>
            <th class="ps-3" style="width:18%">Patient</th>
            <th style="width:28%">Problem</th>
            <th style="width:20%">Watching</th>
            <th style="width:20%">Due</th>
            <th style="width:10%">State</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    body.innerHTML = `<p class="text-danger text-center py-2 mb-0 small">Error loading next-checks: ${escHtml(String(e))}</p>`;
    console.error('loadAuditNextChecks', e);
  }
}

function fmtDue(date) {
  return date.toLocaleTimeString('en-IN', {timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit'});
}

async function toggleNcDetail(detailId, cpmrn, encounter, problemName, activeKey) {
  const row = document.getElementById(detailId);
  if (!row) return;
  const cell = row.querySelector('td');
  if (row.style.display !== 'none') {
    row.style.display = 'none';
    return;
  }
  row.style.display = '';
  if (cell._loaded) return;
  cell.innerHTML = '<div class="p-3 text-muted small">Loading history…</div>';
  try {
    const d = await apiFetch(`/api/audit/patient/${encodeURIComponent(cpmrn)}/${encounter}`);
    const patProbs = d.patient_problems || [];
    const prob = patProbs.find(p => p.problem_name === problemName);
    cell._prob = prob;
    cell._problemName = problemName;
    cell._activeKey = activeKey;
    cell.innerHTML = buildNcProblemHistory(prob, problemName, activeKey, false, detailId);
    cell._loaded = true;
  } catch (e) {
    cell.innerHTML = `<div class="p-3 text-danger small">Error loading history: ${escHtml(String(e))}</div>`;
  }
}

function toggleNcFullHistory(detailId, showFull) {
  const row = document.getElementById(detailId);
  const cell = row && row.querySelector('td');
  if (!cell || !cell._loaded) return;
  cell.innerHTML = buildNcProblemHistory(cell._prob, cell._problemName, cell._activeKey, showFull, detailId);
}

// An "episode" is the run of most-recent assessments that share the currently
// active next_check target — walking backward, it ends the moment an older
// assessment was watching a *different* non-null key (a distinct prior concern).
function _episodeCutoffIndex(assessments, activeKey) {
  if (!activeKey) return assessments.length;
  for (let idx = 0; idx < assessments.length; idx++) {
    const nc = assessments[idx].next_check;
    const key = nc ? (nc.key || nc.type || '') : '';
    if (key && key !== activeKey) return idx;
  }
  return assessments.length;
}

function buildNcProblemHistory(prob, problemName, activeKey, showFull, detailId) {
  if (!prob) return `<div class="p-3 text-muted small">No stored history for "${escHtml(problemName)}".</div>`;

  const allAssessments = (prob.assessments || []).slice().reverse(); // newest first
  if (!allAssessments.length) return '<div class="p-3 text-muted small">No assessment history yet.</div>';

  const cutoff = _episodeCutoffIndex(allAssessments, activeKey);
  const hiddenCount = allAssessments.length - cutoff;
  const assessments = showFull ? allAssessments : allAssessments.slice(0, cutoff);

  const isScoped = !showFull && hiddenCount > 0;
  const episodeStartAt = isScoped && assessments.length ? assessments[assessments.length - 1].assessed_at : null;
  const firstDetected = isScoped && episodeStartAt
    ? `<span class="text-muted" style="font-size:0.72rem">Episode started: ${escHtml(new Date(episodeStartAt).toLocaleString('en-IN', {timeZone:'Asia/Kolkata'}))}</span>`
    : prob.first_detected_at
      ? `<span class="text-muted" style="font-size:0.72rem">First detected: ${escHtml(new Date(prob.first_detected_at).toLocaleString('en-IN', {timeZone:'Asia/Kolkata'}))}</span>`
      : '';

  const rows = assessments.map(a => {
    const ts = a.assessed_at
      ? new Date(a.assessed_at).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata', hour:'2-digit', minute:'2-digit', day:'2-digit', month:'short'})
      : '—';
    const alertBadge = a.alerted
      ? '<span class="badge bg-danger ms-1" style="font-size:0.62rem">alerted</span>'
      : '';
    // Distinguish two suppression modes:
    // cooldown: model wanted to alert (should_alert=true) but code blocked it
    // being_addressed: model itself said no alert (being_addressed, plan documented)
    const suppressedBadge = (!a.alerted && a.should_alert === true)
      ? '<span class="badge ms-1" style="font-size:0.62rem;background:#6f42c1;color:#fff">cooldown</span>'
      : (!a.alerted && a.should_alert === false && a.being_addressed)
        ? '<span class="badge bg-secondary ms-1" style="font-size:0.62rem">plan active</span>'
        : '';
    const nc = a.next_check;
    let ncBadge = '';
    if (nc) {
      const dueStr = nc.due_after
        ? new Date(nc.due_after).toLocaleTimeString('en-IN', {timeZone:'Asia/Kolkata', hour:'2-digit', minute:'2-digit'})
        : null;
      const dueLabel = dueStr ? ` · ${dueStr}` : '';
      ncBadge = `<span class="badge bg-warning text-dark ms-1" style="font-size:0.62rem">next: ${escHtml(nc.label || nc.key || nc.type || '?')}${escHtml(dueLabel)}</span>`;
    }
    const reasoning = a.tracker_reasoning || a.addressed_evidence || a.alert_reason || '';
    return `<tr style="font-size:0.75rem; vertical-align:top">
      <td style="white-space:nowrap; padding:4px 8px; color:#6c757d">${escHtml(ts)}</td>
      <td style="padding:4px 8px; white-space:nowrap">
        <span class="badge ${statusBadgeClass(a.clinical_status)}" style="font-size:0.62rem">${escHtml(a.clinical_status || '?')}</span>
        ${alertBadge}${suppressedBadge}${ncBadge}
      </td>
      <td style="padding:4px 8px; color:#444; line-height:1.35; word-break:break-word">${escHtml(reasoning)}</td>
    </tr>`;
  }).join('');

  const scopeLabel = (!showFull && activeKey)
    ? `<span class="text-muted" style="font-size:0.72rem">— current episode (watching ${escHtml(activeKey)})</span>`
    : (showFull ? '<span class="text-muted" style="font-size:0.72rem">— full history</span>' : '');

  const toggleLink = hiddenCount > 0
    ? (showFull
        ? `<a href="#" class="ms-auto" style="font-size:0.72rem" onclick="event.preventDefault(); toggleNcFullHistory('${detailId}', false)">Show current episode only</a>`
        : `<a href="#" class="ms-auto" style="font-size:0.72rem" onclick="event.preventDefault(); toggleNcFullHistory('${detailId}', true)">Show full history (${hiddenCount} earlier assessment${hiddenCount === 1 ? '' : 's'} from a different watch target)</a>`)
    : '';

  return `<div style="padding:10px 14px">
    <div class="d-flex align-items-baseline gap-3 mb-2">
      <span class="fw-semibold" style="font-size:0.82rem">${escHtml(problemName)} — history</span>
      ${scopeLabel}
      ${firstDetected}
      ${toggleLink}
    </div>
    <div class="table-responsive">
      <table class="table table-sm mb-0" style="border:1px solid #dee2e6">
        <thead style="font-size:0.7rem; color:#6c757d; background:#f1f3f5">
          <tr>
            <th style="padding:4px 8px; white-space:nowrap">Time (IST)</th>
            <th style="padding:4px 8px">Status</th>
            <th style="padding:4px 8px">Reasoning / evidence</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  </div>`;
}

function fmtRunTs(isoStr) {
  // Format run timestamp in IST (Asia/Kolkata) — UTC is hard to parse at a glance
  try {
    return new Date(isoStr).toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      year:     'numeric',
      month:    '2-digit',
      day:      '2-digit',
      hour:     '2-digit',
      minute:   '2-digit',
      hour12:   false,
    }).replace(',', '');   // "25/06/2026 06:04" — drop the comma locale inserts
  } catch {
    return isoStr.slice(0, 16).replace('T', ' ');
  }
}

// ── Run picker + patient audit table ─────────────────────────────────────────

async function loadAuditRuns() {
  const picker = document.getElementById('audit-run-picker');
  const tbody  = document.querySelector('#tbl-audit tbody');
  if (!picker || !tbody) return;

  try {
    const rows = await apiFetch('/api/metrics/runs');
    if (!rows.length) {
      picker.innerHTML = '<option value="">No runs found</option>';
      tbody.innerHTML  = '<tr><td colspan="7" class="text-muted text-center py-3">No runs found.</td></tr>';
      return;
    }

    picker.innerHTML = rows.map((r, idx) => {
      const ts    = fmtRunTs(r.run_started_at) + ' IST';
      const label = idx === 0
        ? `${ts}  (latest · ${fmt(r.expensive_count || 0)} exp, ${fmt(r.alerts_sent)} alerts)`
        : `${ts}  (${fmt(r.expensive_count || 0)} exp, ${fmt(r.alerts_sent)} alerts)`;
      return `<option value="${escHtml(r.run_started_at)}">${escHtml(label)}</option>`;
    }).join('');

    // Auto-load latest run
    const latest = rows[0];
    updateAuditRunSummary(latest);
    await loadAuditRunPatients(latest.run_started_at);

    picker.addEventListener('change', async e => {
      const chosen = rows.find(r => r.run_started_at === e.target.value);
      if (chosen) updateAuditRunSummary(chosen);
      await loadAuditRunPatients(e.target.value);
    });
  } catch (e) {
    picker.innerHTML = '<option value="">Error loading runs</option>';
    tbody.innerHTML  = `<tr><td colspan="7" class="text-danger text-center py-3">Error: ${escHtml(String(e))}</td></tr>`;
    console.error('loadAuditRuns', e);
  }
}

function updateAuditRunSummary(run) {
  const el = document.getElementById('audit-run-summary');
  if (!el || !run) return;
  const parts = [];
  if (run.total_scheduled != null) parts.push(fmt(run.total_scheduled) + ' scheduled');
  if (run.expensive_count  != null) parts.push(fmt(run.expensive_count) + ' expensive');
  if (run.alerts_sent      != null) parts.push(fmt(run.alerts_sent) + ' alerts');
  if (run.cost_usd         != null) parts.push(usdShort(run.cost_usd));
  el.textContent = parts.join(' · ');
}

async function loadAuditRunPatients(runTs) {
  const tbody = document.querySelector('#tbl-audit tbody');
  if (!tbody) return;
  if (!runTs) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-muted text-center py-3">Select a run above.</td></tr>';
    return;
  }
  tbody.innerHTML = '<tr><td colspan="7" class="text-center py-3"><div class="spinner-border spinner-border-sm text-primary"></div></td></tr>';
  try {
    const rows = await apiFetch('/api/metrics/run-audit?run=' + encodeURIComponent(runTs));
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-muted text-center py-3">No audit data for this run.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(r => auditPatientRow(r)).join('');
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="text-danger text-center py-3">Error: ${escHtml(String(e))}</td></tr>`;
    console.error('loadAuditRunPatients', e);
  }
}

function auditPatientRow(r) {
  const pass1Label = r.pass1_tag === 'upstream_gate'
    ? '<span class="badge bg-info bg-opacity-75 text-dark" style="font-size:0.7rem">lab/vital gate</span>'
    : r.pass1_needs_full
      ? `<span class="badge bg-danger bg-opacity-75" style="font-size:0.7rem">flagged</span>`
      : `<span class="badge bg-secondary bg-opacity-25 text-secondary" style="font-size:0.7rem">cheap</span>`;

  // Extract the decisive skip step and skip_gate breakdown from gate_trace
  const _gateSkipInfo = (() => {
    const trace = r.gate_trace || [];
    // Find the first step that caused a stop (skip/normal on entry gates, or combined skip)
    const decisiveSkip = trace.find(s =>
      (s.phase === 'entry' && s.verdict === 'skip') ||
      (s.phase === 'skip_gate' && s.gate === 'combined' && s.verdict === 'skip')
    );
    // Collect skip_gate phase individual verdicts (vitals/abg/labs/notes)
    const skipGateSteps = trace.filter(s => s.phase === 'skip_gate' && s.gate !== 'combined');
    return { decisiveSkip, skipGateSteps };
  })();

  const pass2Label = (() => {
    const p = r.pass2_outcome || '';
    let badge = '';
    if (p === 'skipped_by_pass1')             badge = '<span class="badge bg-secondary bg-opacity-20 text-muted" style="font-size:0.7rem">skipped</span>';
    else if (p === 'forced_by_overdue_next_check') badge = '<span class="badge bg-warning text-dark" style="font-size:0.7rem">forced (overdue)</span>';
    else if (p === 'upstream_forced')         badge = '<span class="badge bg-danger bg-opacity-75" style="font-size:0.7rem">expensive (gate forced)</span>';
    else if (p.startsWith('pass2:'))          badge = `<span class="badge bg-primary bg-opacity-75" style="font-size:0.7rem">${escHtml(p.replace('pass2:',''))}</span>`;
    else if (p)                               badge = `<span class="badge bg-secondary" style="font-size:0.7rem">${escHtml(p)}</span>`;
    else {
      // No pass2_outcome recorded — derive from gate_trace
      const { decisiveSkip, skipGateSteps } = _gateSkipInfo;
      if (decisiveSkip) {
        const gateNames = { empty_chart: 'empty chart', cadence: 'cadence', delta: 'no new data', combined: 'all normal' };
        const label = gateNames[decisiveSkip.gate] || decisiveSkip.gate;
        badge = `<span class="badge bg-secondary bg-opacity-20 text-secondary" style="font-size:0.7rem">skip: ${escHtml(label)}</span>`;
        if (decisiveSkip.detail) {
          badge += `<div class="text-muted mt-1" style="font-size:0.68rem;line-height:1.3">${escHtml(fmtGateDetail(decisiveSkip.detail))}</div>`;
        }
        // Skip gate: show which categories were evaluated
        if (skipGateSteps.length) {
          const catBadges = skipGateSteps.map(s => {
            const cls = s.verdict === 'significant'       ? 'bg-danger bg-opacity-50 text-dark'
                      : s.verdict === 'normal'            ? 'bg-success bg-opacity-20 text-success'
                      : s.verdict === 'news2_stable_skip' ? 'bg-warning bg-opacity-75 text-dark'
                      : 'bg-secondary bg-opacity-20 text-muted';
            const label = s.verdict === 'news2_stable_skip' ? 'vitals: NEWS2 stable skip' : `${s.gate}: ${s.verdict}`;
            const title = s.detail ? ` title="${escHtml(s.detail)}"` : '';
            return `<span class="badge ${cls}" style="font-size:0.6rem;cursor:default"${title}>${escHtml(label)}</span>`;
          }).join(' ');
          badge += `<div class="mt-1 d-flex flex-wrap gap-1">${catBadges}</div>`;
        }
      } else {
        badge = '<span class="text-muted" style="font-size:0.8rem">—</span>';
      }
    }

    const scoped = r.problems_scoped;
    if (scoped && scoped.length) {
      const pills = scoped.map(n =>
        `<span class="badge bg-primary bg-opacity-10 text-primary border border-primary border-opacity-25" style="font-size:0.62rem;font-weight:500">${escHtml(n)}</span>`
      ).join(' ');
      badge += `<div class="mt-1" style="font-size:0.65rem;color:#6c757d;line-height:1.4">scope<br>${pills}</div>`;
    }
    return badge;
  })();

  const triggerReasonHtml = (() => {
    const scheduling = r.trigger_reason || '';
    const screener   = r.pass1_needs_full ? (r.pass1_tag || '') : '';
    if (!scheduling && !screener) return '<span class="text-muted" style="font-size:0.8rem">—</span>';
    const parts = [];
    if (scheduling) parts.push(
      `<div style="font-size:0.75rem; margin-bottom:2px">` +
      `<span class="text-muted" style="font-weight:600">Scheduling: </span>` +
      `<span class="text-muted">${escHtml(scheduling)}</span></div>`
    );
    if (screener) parts.push(
      `<div style="font-size:0.75rem">` +
      `<span class="text-muted" style="font-weight:600">Screener: </span>` +
      `<span class="text-muted">${escHtml(screener)}</span></div>`
    );
    return parts.join('');
  })();

  const problems = r.problems || [];
  const problemsHtml = problems.length
    ? problems.map(p => {
        const reasoning = p.tracker_reasoning || p.alert_reason || '';
        return `<div style="font-size:0.78rem; margin-bottom:3px">
          <strong>${escHtml(p.problem_name || '')}</strong>
          <span class="text-muted ms-1" style="font-size:0.72rem">[${escHtml(p.clinical_status || '?')}]</span>
          ${suppressionBadge(p.suppression_rule)}
          ${reasoning ? `<div class="text-muted mt-1" style="font-size:0.72rem;line-height:1.3">${escHtml(reasoning)}</div>` : ''}
        </div>`;
      }).join('')
    : '<span class="text-muted small">—</span>';

  const outcomeBadge = (() => {
    const o = r.pipeline_outcome || '';
    if (o === 'alerted')            return '<span class="badge bg-danger" style="font-size:0.7rem">alerted</span>';
    if (o === 'expensive_no_alert') return '<span class="badge bg-warning text-dark" style="font-size:0.7rem">expensive, no alert</span>';
    if (o === 'cheap')              return '<span class="badge bg-secondary bg-opacity-25 text-secondary" style="font-size:0.7rem">cheap</span>';
    if (o.startsWith('error'))      return `<span class="badge bg-dark" style="font-size:0.7rem">${escHtml(o)}</span>`;
    return `<span class="badge bg-secondary" style="font-size:0.7rem">${escHtml(o)}</span>`;
  })();

  const dv = r.delta_vitals || 0, dl = r.delta_labs || 0, dn = r.delta_notes || 0, dr = r.delta_reports || 0;
  const deltaHtml = (() => {
    if (dv + dl + dn + dr > 0) {
      return `<span style="font-size:0.72rem;line-height:1.6">` +
        (dv ? `<span class="text-primary">${dv}v</span> ` : '') +
        (dl ? `<span class="text-success">${dl}l</span> ` : '') +
        (dn ? `<span class="text-warning">${dn}n</span> ` : '') +
        (dr ? `<span class="text-info">${dr}r</span>` : '') +
        `</span>`;
    }
    // Extract delta cutoff from gate_trace delta step detail ("no new data since last LLM run" or entry delta step)
    const trace = r.gate_trace || [];
    const deltaStep = trace.find(s => s.gate === 'delta');
    const emptyStep = trace.find(s => s.gate === 'empty_chart' && s.verdict === 'skip');
    if (emptyStep) {
      return `<span class="text-muted" style="font-size:0.68rem">empty chart</span>`;
    }
    if (deltaStep && deltaStep.verdict === 'skip') {
      // Try to pull cutoff from detail string e.g. "no new data since last LLM run"
      return `<span class="text-muted" style="font-size:0.68rem">no new data</span>`;
    }
    // Skip gate fired (all categories normal) — had data but all within normal range
    const combinedStep = trace.find(s => s.phase === 'skip_gate' && s.gate === 'combined' && s.verdict === 'skip');
    if (combinedStep) {
      return `<span style="font-size:0.68rem;line-height:1.6">` +
        (dv ? `<span class="text-primary">${dv}v</span> ` : '') +
        (dl ? `<span class="text-success">${dl}l</span> ` : '') +
        (dn ? `<span class="text-warning">${dn}n</span> ` : '') +
        (dr ? `<span class="text-info">${dr}r</span>` : '') +
        `<span class="text-muted d-block" style="font-size:0.65rem">all normal</span></span>`;
    }
    return '<span class="text-muted" style="font-size:0.72rem">—</span>';
  })();

  const rowId = `audit-row-${escHtml(r.CPMRN)}-${r.encounter}`;
  // Store gate_trace in a global map so togglePatientDetail can read it
  (window._auditGateTraces = window._auditGateTraces || new Map())
    .set(`${r.CPMRN}:${r.encounter}`, r.gate_trace || []);
  const gateTraceStrip = buildGateTraceStrip(r.gate_trace);
  return `<tr id="${rowId}" data-cpmrn="${escHtml(r.CPMRN)}" data-enc="${r.encounter || 1}">
    <td class="align-top" style="white-space:nowrap">
      <span class="fw-semibold" style="font-size:0.82rem">${escHtml(r.CPMRN)}</span><br>
      ${outcomeBadge}
    </td>
    <td class="align-top">${pass1Label}</td>
    <td class="align-top">${triggerReasonHtml}</td>
    <td class="align-top">${pass2Label}</td>
    <td class="align-top">${deltaHtml}</td>
    <td class="align-top">${problemsHtml}${gateTraceStrip}</td>
    <td class="align-top text-end">
      <button class="btn btn-sm btn-outline-secondary py-0 px-1 expand-btn" style="font-size:0.72rem"
              onclick="togglePatientDetail(this)" title="Show model detail">
        <i class="bi bi-chevron-down"></i>
      </button>
    </td>
  </tr>`;
}

// Convert "not due until HH:MM UTC" → "not due until HH:MM IST" in gate detail strings.
// Handles both stored-UTC strings (old runs) and already-converted IST strings (new runs).
function fmtGateDetail(detail) {
  if (!detail) return detail;
  return detail.replace(/not due until (\d{2}):(\d{2}) UTC/g, (_, hh, mm) => {
    const d = new Date();
    d.setUTCHours(parseInt(hh, 10), parseInt(mm, 10), 0, 0);
    const ist = d.toLocaleTimeString('en-IN', {timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false});
    return `not due until ${ist} IST`;
  });
}

function buildGateTraceStrip(trace) {
  if (!trace || !trace.length) return '';
  const phaseLabels = {entry: 'Entry', skip_gate: 'Skip gate', triage: 'Triage', analysis: 'Analysis'};
  const phaseOrder  = ['entry', 'skip_gate', 'triage', 'analysis'];
  const byPhase = {};
  for (const step of trace) {
    if (!byPhase[step.phase]) byPhase[step.phase] = [];
    byPhase[step.phase].push(step);
  }
  const parts = phaseOrder
    .filter(p => byPhase[p])
    .map(p => {
      const badges = byPhase[p].map(s =>
        `<span class="badge gt-${escHtml(s.verdict)}" title="${escHtml(s.gate + (s.detail ? ': ' + fmtGateDetail(s.detail) : ''))}">${escHtml(s.gate)}</span>`
      ).join(' ');
      return `<span class="gt-phase-label">${escHtml(phaseLabels[p] || p)}</span>${badges}`;
    })
    .join('<span class="gt-sep">·</span>');
  return `<div class="gate-trace-strip mt-2">${parts}</div>`;
}

// ── Per-patient lazy drill-down ───────────────────────────────────────────────

async function togglePatientDetail(btn) {
  const patientTr = btn.closest('tr');
  const detailId  = 'detail-' + patientTr.id;
  const existing  = document.getElementById(detailId);

  if (existing) {
    existing.remove();
    btn.innerHTML = '<i class="bi bi-chevron-down"></i>';
    return;
  }

  btn.innerHTML = '<div class="spinner-border spinner-border-sm" style="width:12px;height:12px"></div>';

  const cpmrn = patientTr.dataset.cpmrn;
  const enc   = patientTr.dataset.enc || 1;

  // Placeholder row while loading
  const placeholderTr = document.createElement('tr');
  placeholderTr.id = detailId;
  placeholderTr.innerHTML = `<td colspan="7" class="p-0">
    <div class="text-center py-3 small text-muted">
      <div class="spinner-border spinner-border-sm text-primary me-2"></div>Loading model detail…
    </div>
  </td>`;
  patientTr.insertAdjacentElement('afterend', placeholderTr);

  try {
    const d = await apiFetch(`/api/audit/patient/${encodeURIComponent(cpmrn)}/${enc}`);
    const gateTrace = (window._auditGateTraces || new Map()).get(`${cpmrn}:${enc}`) || [];
    placeholderTr.innerHTML = `<td colspan="7" class="p-0">${buildDetailPanel(d, detailId, gateTrace)}</td>`;
    // Activate first tab
    const firstTab = placeholderTr.querySelector('[data-tab]');
    if (firstTab) switchAuditTab(firstTab, detailId);
    btn.innerHTML = '<i class="bi bi-chevron-up"></i>';
  } catch (e) {
    placeholderTr.innerHTML = `<td colspan="7" class="text-danger text-center py-2 small">
      Error loading detail: ${escHtml(String(e))}
    </td>`;
    btn.innerHTML = '<i class="bi bi-chevron-down"></i>';
    console.error('togglePatientDetail', e);
  }
}

function buildDetailPanel(d, panelId, gateTrace) {
  const summaryHtml       = buildSummaryTab(d);
  const problemsHtml      = buildProblemsTab(d);
  const reasoningHtml     = buildReasoningTab(d);
  const deltaHtml         = buildDeltaTab(d);
  const timelineHtml      = buildTimelineTab(d);
  const pipelineStepsHtml = buildPipelineStepsTab(gateTrace || []);

  return `<div style="background:#f8f9fa; border-top:1px solid #dee2e6; padding:10px 14px">
    <div class="d-flex gap-3 border-bottom mb-2 pb-1" style="font-size:0.82rem">
      <button class="btn btn-link btn-sm p-0 fw-semibold text-decoration-none"
              data-tab="summary" data-panel="${panelId}"
              onclick="switchAuditTab(this,'${panelId}')">Summary</button>
      <button class="btn btn-link btn-sm p-0 text-muted text-decoration-none"
              data-tab="problems" data-panel="${panelId}"
              onclick="switchAuditTab(this,'${panelId}')">Problems &amp; next-checks</button>
      <button class="btn btn-link btn-sm p-0 text-muted text-decoration-none"
              data-tab="reasoning" data-panel="${panelId}"
              onclick="switchAuditTab(this,'${panelId}')">Reasoning history</button>
      <button class="btn btn-link btn-sm p-0 text-muted text-decoration-none"
              data-tab="delta" data-panel="${panelId}"
              onclick="switchAuditTab(this,'${panelId}')">Last delta</button>
      <button class="btn btn-link btn-sm p-0 text-muted text-decoration-none"
              data-tab="timeline" data-panel="${panelId}"
              onclick="switchAuditTab(this,'${panelId}')">Timeline</button>
      <button class="btn btn-link btn-sm p-0 text-muted text-decoration-none"
              data-tab="pipeline" data-panel="${panelId}"
              onclick="switchAuditTab(this,'${panelId}')">Pipeline steps</button>
      <span class="ms-auto text-muted" style="font-size:0.72rem">lazy · loaded on expand</span>
    </div>
    <div data-pane="summary" data-panel="${panelId}">${summaryHtml}</div>
    <div data-pane="problems" data-panel="${panelId}" style="display:none">${problemsHtml}</div>
    <div data-pane="reasoning" data-panel="${panelId}" style="display:none">${reasoningHtml}</div>
    <div data-pane="delta" data-panel="${panelId}" style="display:none">${deltaHtml}</div>
    <div data-pane="timeline" data-panel="${panelId}" style="display:none">${timelineHtml}</div>
    <div data-pane="pipeline" data-panel="${panelId}" style="display:none">${pipelineStepsHtml}</div>
  </div>`;
}

function buildPipelineStepsTab(trace) {
  if (!trace || !trace.length) {
    return '<div class="text-muted small py-2">No gate trace for this run (pre-refactor run or run that skipped before Phase 0).</div>';
  }
  const phaseLabels = {entry: 'Entry', skip_gate: 'Skip Gate', triage: 'Triage', analysis: 'Analysis'};
  let html = '<div style="font-size:0.82rem">';
  let lastPhase = null;
  for (const step of trace) {
    if (step.phase !== lastPhase) {
      if (lastPhase !== null) html += '</div>';
      html += `<div class="mb-3">
        <div class="text-muted mb-1" style="font-size:0.7rem;text-transform:uppercase;letter-spacing:.06em;font-weight:600">
          ${escHtml(phaseLabels[step.phase] || step.phase)}
        </div>`;
      lastPhase = step.phase;
    }
    html += `<div class="d-flex align-items-baseline gap-2 mb-1">
      <span class="badge gt-${escHtml(step.verdict)}" style="min-width:72px;text-align:center;flex-shrink:0">${escHtml(step.verdict)}</span>
      <span class="fw-semibold text-dark" style="min-width:110px;flex-shrink:0;font-size:0.8rem">${escHtml(step.gate)}</span>
      <span class="text-muted" style="font-size:0.78rem">${escHtml(step.detail || '')}</span>
    </div>`;
  }
  if (lastPhase !== null) html += '</div>';
  html += '</div>';
  return html;
}

function switchAuditTab(btn, panelId) {
  const detailTr = document.getElementById(panelId);
  if (!detailTr) return;
  // Deactivate all tabs
  detailTr.querySelectorAll('[data-tab]').forEach(b => {
    b.classList.remove('fw-semibold');
    b.classList.add('text-muted');
  });
  btn.classList.add('fw-semibold');
  btn.classList.remove('text-muted');
  // Show the right pane
  const tabName = btn.dataset.tab;
  detailTr.querySelectorAll('[data-pane]').forEach(p => {
    p.style.display = p.dataset.pane === tabName ? '' : 'none';
  });
}

function buildSummaryTab(d) {
  const lw  = d.lightweight_summary || '';
  const run = d.running_summary || '';
  const nar = d.narrative || '';
  const actions = d.suggested_actions || [];

  const lwHtml  = lw  ? `<div class="mb-2"><span class="text-muted" style="font-size:0.72rem">LIGHTWEIGHT SUMMARY</span><div style="font-size:0.82rem">${escHtml(lw)}</div></div>` : '';
  const narHtml = nar ? `<div class="mb-2"><span class="text-muted" style="font-size:0.72rem">NARRATIVE</span><div style="font-size:0.82rem">${escHtml(nar)}</div></div>` : '';
  const runHtml = run ? `<div class="mb-2"><span class="text-muted" style="font-size:0.72rem">RUNNING SUMMARY</span><div style="font-size:0.82rem;white-space:pre-wrap">${escHtml(run.slice(0, 600))}${run.length > 600 ? '…' : ''}</div></div>` : '';
  const actHtml = actions.length
    ? `<div class="mb-1"><span class="text-muted" style="font-size:0.72rem">SUGGESTED ACTIONS</span>
       <ul class="mb-0 ps-3" style="font-size:0.82rem">${actions.map(a => `<li>${escHtml(typeof a === 'string' ? a : (a.action || JSON.stringify(a)))}</li>`).join('')}</ul></div>`
    : '';

  return lwHtml + narHtml + runHtml + actHtml || '<span class="text-muted small">No summary data for this run (latest run only).</span>';
}

function buildProblemsTab(d) {
  const probs   = d.problems || [];
  const resolved = d.resolved_problems || [];
  const patProbs = d.patient_problems || [];

  let html = '';

  if (probs.length) {
    html += `<div class="mb-2"><span class="text-muted" style="font-size:0.72rem">ACTIVE PROBLEMS (${probs.length})</span>`;
    html += probs.map(p => {
      const nc = patProbs.find(pp => pp.problem_name === p.name);
      const ncInfo = nc && nc.next_check
        ? ` <span class="badge bg-warning text-dark ms-1" style="font-size:0.68rem">next-check: ${escHtml(nc.next_check.label || nc.next_check.key || nc.next_check.type || '')}</span>`
        : '';
      return `<div style="font-size:0.82rem; padding:3px 0; border-bottom:1px solid #e9ecef">
        <strong>${escHtml(p.name || '')}</strong>
        <span class="badge ${statusBadgeClass(p.status)} ms-1" style="font-size:0.68rem">${escHtml(p.status || '')}</span>
        ${ncInfo}
        ${p.current_state ? `<div class="text-muted" style="font-size:0.75rem">${escHtml(p.current_state)}</div>` : ''}
      </div>`;
    }).join('');
    html += '</div>';
  }

  if (resolved.length) {
    html += `<div class="mb-2"><span class="text-muted" style="font-size:0.72rem">RESOLVED (${resolved.length})</span>`;
    html += `<div style="font-size:0.8rem; color:#6c757d">${resolved.map(p => escHtml(p.name || p)).join(', ')}</div></div>`;
  }

  // Problems with active next_check from patient_problems
  const withNc = patProbs.filter(p => p.next_check);
  if (withNc.length) {
    html += `<div class="mb-1"><span class="text-muted" style="font-size:0.72rem">ACTIVE NEXT-CHECKS (${withNc.length})</span>`;
    html += withNc.map(p => {
      const nc  = p.next_check;
      const due = nc.due_after ? new Date(nc.due_after) : null;
      const now = new Date();
      const overdue = due && now > due;
      const fmtTime = d => d.toLocaleTimeString('en-IN', {timeZone:'Asia/Kolkata', hour:'2-digit', minute:'2-digit'});
      const fmtAge = ms => { const m = Math.round(ms/60000); return m >= 60 ? `${(m/60).toFixed(1)}h` : `${m}m`; };
      const dueHtml = due
        ? `<span class="${overdue ? 'text-danger fw-semibold' : 'text-warning'}" style="font-size:0.75rem"> · recheck ${overdue ? 'overdue since' : 'due'} ${fmtTime(due)}${overdue ? ` (${fmtAge(now-due)} ago)` : ''}</span>`
        : '';
      // Show cooldown window: alert can't re-fire until last_alerted_at + current_interval_h
      let cooldownHtml = '';
      if (p.last_alerted_at && p.current_interval_h) {
        const cooldownUntil = new Date(new Date(p.last_alerted_at).getTime() + p.current_interval_h * 3600000);
        if (cooldownUntil > now) {
          const minLeft = Math.round((cooldownUntil - now) / 60000);
          cooldownHtml = `<span style="font-size:0.72rem;color:#6f42c1"> · in cooldown — alert sendable at ${fmtTime(cooldownUntil)} (${minLeft}m)</span>`;
        }
      }
      return `<div style="font-size:0.8rem; padding:2px 0">
        <strong>${escHtml(p.problem_name)}</strong> →
        watching <em>${escHtml(nc.label || nc.key || nc.type || '?')}</em>
        ${dueHtml}${cooldownHtml}
      </div>`;
    }).join('');
    html += '</div>';
  }

  return html || '<span class="text-muted small">No problems data (latest run only).</span>';
}

function buildReasoningTab(d) {
  const patProbs = d.patient_problems || [];
  if (!patProbs.length) return '<span class="text-muted small">No reasoning data (latest run only).</span>';

  return patProbs.map(p => {
    const assessments = (p.assessments || []).slice().reverse(); // newest first
    if (!assessments.length) return '';
    const rows = assessments.slice(0, 10).map(a => {
      const ts = a.assessed_at ? new Date(a.assessed_at).toLocaleString('en-IN', {timeZone:'Asia/Kolkata', hour:'2-digit', minute:'2-digit'}) : '—';
      const alerted = a.alerted ? '<span class="badge bg-danger ms-1" style="font-size:0.65rem">alerted</span>' : '';
      const suppressed = (!a.alerted && a.should_alert === true)
        ? '<span class="badge ms-1" style="font-size:0.65rem;background:#6f42c1;color:#fff">cooldown</span>'
        : (!a.alerted && a.should_alert === false && a.being_addressed)
          ? '<span class="badge bg-secondary ms-1" style="font-size:0.65rem">plan active</span>' : '';
      const reason = a.addressed_evidence || a.alert_reason || '';
      return `<tr style="font-size:0.76rem">
        <td class="text-muted" style="white-space:nowrap; padding:3px 6px">${escHtml(ts)}</td>
        <td style="padding:3px 6px">
          <span class="badge ${statusBadgeClass(a.clinical_status)}" style="font-size:0.65rem">${escHtml(a.clinical_status || '?')}</span>
          ${alerted}${suppressed}
        </td>
        <td style="padding:3px 6px; font-size:0.75rem; color:#555; white-space:pre-wrap; word-break:break-word">${escHtml(reason)}</td>
      </tr>`;
    }).join('');

    return `<div class="mb-3">
      <div class="fw-semibold mb-1" style="font-size:0.82rem">${escHtml(p.problem_name)}</div>
      <div class="table-responsive">
        <table class="table table-sm mb-0" style="border:1px solid #dee2e6">
          <thead style="font-size:0.72rem; color:#6c757d; background:#f1f3f5">
            <tr><th style="padding:3px 6px">Assessed at</th><th style="padding:3px 6px">Status</th><th style="padding:3px 6px">Reasoning</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>`;
  }).join('') || '<span class="text-muted small">No assessment history found.</span>';
}

function buildDeltaTab(d) {
  const dc = d.last_delta_content || {};
  const runAt = d.last_llm_run_at ? new Date(d.last_llm_run_at).toLocaleString() : null;
  const vitals = dc.vitals || [];
  const labs   = dc.labs   || [];
  const notes  = dc.notes  || [];
  const io     = dc.io_last_24h;
  const ioChanged = dc.io_changed;

  if (!vitals.length && !labs.length && !notes.length && !ioChanged) {
    return '<span class="text-muted small">No delta content stored yet — will appear after the next pipeline run.</span>';
  }

  const VITAL_LABELS = {
    daysHR: 'HR', daysBP: 'BP', daysMAP: 'MAP', daysSpO2: 'SpO₂',
    daysRR: 'RR', daysFiO2: 'FiO₂', daysTemp: 'Temp', daysGCS: 'GCS',
  };

  let html = '';
  if (runAt) html += `<div class="text-muted mb-2" style="font-size:0.72rem">Last LLM run: ${escHtml(runAt)}</div>`;

  if (vitals.length) {
    html += `<div class="mb-3"><span class="text-primary fw-semibold" style="font-size:0.78rem">Vitals (${vitals.length})</span><div class="mt-1">`;
    vitals.forEach(v => {
      const ts = v.timestamp ? new Date(v.timestamp).toLocaleString() : '?';
      const parts = Object.entries(VITAL_LABELS)
        .filter(([k]) => v[k] != null && v[k] !== '')
        .map(([k, label]) => `<span class="me-2"><span class="text-muted" style="font-size:0.7rem">${label}</span> <strong style="font-size:0.78rem">${escHtml(String(v[k]))}</strong></span>`)
        .join('');
      html += `<div style="font-size:0.78rem; padding:3px 0; border-bottom:1px solid #e9ecef">
        <span class="text-muted me-2" style="font-size:0.7rem">${escHtml(ts)}</span>${parts || '<span class="text-muted">no values</span>'}
      </div>`;
    });
    html += '</div></div>';
  }

  if (labs.length) {
    html += `<div class="mb-3"><span class="text-success fw-semibold" style="font-size:0.78rem">Labs (${labs.length})</span><div class="mt-1">`;
    labs.forEach(lab => {
      const ts = lab.reportedAt ? new Date(lab.reportedAt).toLocaleString() : '?';
      const vals = Object.entries(lab.values || {})
        .map(([k, v]) => `<span class="me-2"><span class="text-muted" style="font-size:0.7rem">${escHtml(k)}</span> <strong style="font-size:0.78rem">${escHtml(String(v))}</strong></span>`)
        .join('');
      html += `<div style="font-size:0.78rem; padding:3px 0; border-bottom:1px solid #e9ecef">
        <span class="fw-semibold me-1">${escHtml(lab.name || '?')}</span>
        <span class="text-muted me-2" style="font-size:0.7rem">${escHtml(ts)}</span>${vals}
      </div>`;
    });
    html += '</div></div>';
  }

  if (notes.length) {
    html += `<div class="mb-3"><span class="text-warning fw-semibold" style="font-size:0.78rem">Notes (${notes.length})</span><div class="mt-1">`;
    notes.forEach(n => {
      const ts = n.timestamp ? new Date(n.timestamp).toLocaleString() : '?';
      html += `<div style="font-size:0.78rem; padding:4px 0; border-bottom:1px solid #e9ecef">
        <div class="text-muted mb-1" style="font-size:0.7rem">${escHtml(ts)} · ${escHtml(n.note_type || '')}${n.author ? ' · ' + escHtml(n.author) : ''}</div>
        <div style="white-space:pre-wrap">${escHtml(n.text || '')}</div>
      </div>`;
    });
    html += '</div></div>';
  }

  if (ioChanged && io) {
    html += `<div class="mb-2"><span class="text-info fw-semibold" style="font-size:0.78rem">I/O changed</span>
      <div style="font-size:0.78rem; margin-top:4px">
        <span class="me-3">Intake <strong>${io.intake_ml} ml</strong></span>
        <span class="me-3">Output <strong>${io.output_ml} ml</strong></span>
        <span>Balance <strong>${io.balance_ml} ml</strong></span>
      </div>
    </div>`;
  }

  return html;
}

function buildTimelineTab(d) {
  const tl = d.clinical_timeline || {};
  const prior  = tl.prior_course || '';
  const events = tl.recent_events || [];

  if (!prior && !events.length) {
    return '<span class="text-muted small">No timeline yet — will appear after the next expensive pipeline run.</span>';
  }

  const KIND_BADGE = {
    order:          'bg-primary bg-opacity-75',
    finding:        'bg-info text-dark',
    intervention:   'bg-success bg-opacity-75',
    status_change:  'bg-warning text-dark',
  };

  let html = '';

  if (prior) {
    html += `<div class="mb-3 p-2" style="background:#fff3cd; border-radius:6px; border:1px solid #ffc107">
      <div class="text-muted mb-1" style="font-size:0.7rem">PRIOR COURSE (compressed history)</div>
      <div style="font-size:0.82rem; white-space:pre-wrap">${escHtml(prior)}</div>
    </div>`;
  }

  if (events.length) {
    html += `<div class="mb-1"><span class="text-muted" style="font-size:0.7rem">RECENT EVENTS (${events.length})</span></div>`;
    // Sort oldest → newest for display
    const sorted = events.slice().sort((a, b) => (a.t || '').localeCompare(b.t || ''));
    html += sorted.map(ev => {
      const t = ev.t ? new Date(ev.t).toLocaleString('en-IN', {timeZone:'Asia/Kolkata',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).replace(',','') + ' IST' : '?';
      const kindBadge = KIND_BADGE[ev.kind] || 'bg-secondary';
      return `<div style="display:flex; gap:8px; align-items:flex-start; padding:4px 0; border-bottom:1px solid #e9ecef; font-size:0.8rem">
        <div class="text-muted" style="min-width:110px; font-size:0.72rem; padding-top:2px">${escHtml(t)}</div>
        <div style="flex:1">
          <div>
            <span class="badge ${kindBadge}" style="font-size:0.62rem">${escHtml(ev.kind || '')}</span>
            <span class="ms-1 fw-semibold" style="font-size:0.75rem">${escHtml(ev.problem || '')}</span>
          </div>
          <div style="margin-top:2px">${escHtml(ev.event || '')}</div>
          <div class="text-muted" style="font-size:0.68rem">${escHtml(ev.source || '')}</div>
        </div>
      </div>`;
    }).join('');
  }

  return html;
}

function statusBadgeClass(status) {
  if (!status) return 'bg-secondary';
  const s = String(status).toLowerCase();
  if (s === 'critical')  return 'bg-danger';
  if (s === 'worsening') return 'bg-warning text-dark';
  if (s === 'stable')    return 'bg-secondary';
  if (s === 'improving') return 'bg-success';
  return 'bg-secondary';
}
