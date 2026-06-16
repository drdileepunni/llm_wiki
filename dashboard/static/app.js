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
  ['protocols','lab','symptom','ops'].forEach(t => {
    document.getElementById('tab-' + t)?.classList.toggle('d-none', t !== name);
  });
  document.querySelectorAll('#config-tabs .nav-link').forEach(l => l.classList.remove('active'));
  if (linkEl) linkEl.classList.add('active');
}

async function loadConfig() {
  if (window.PAGE !== 'config') return;
  await Promise.all([
    loadProtocols(), loadLabRules(), loadSymptomRules(), loadOpsSettings()
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

async function loadOpsSettings() {
  const el = document.getElementById('ops-content');
  try {
    const d = await apiFetch('/api/config/operational');
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

async function openEditSymptomRules() {
  try {
    const d = await apiFetch('/api/config/symptom-rules');
    _openEditModal('Edit Symptom Alert Rules', d, '/api/config/symptom-rules',
      'Saves to gs://patientview-cds-pipeline-ops/app_settings/symptom_alert_rules.json',
      loadSymptomRules);
  } catch (e) { alert('Could not load current rules: ' + e.message); }
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
      const first = el.querySelector('.doc-link');
      if (first) first.click();
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
});
