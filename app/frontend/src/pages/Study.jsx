import { useEffect, useState, useCallback } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, BarChart, Bar } from 'recharts'
import {
  getStudyQueue, getStudyAlert, submitAdjudication,
  getStudyMetrics, getStudyMetricsHistory,
  getStudyCosts, getStudyCostSummary,
} from '../api'

const STATUS_COLOR = {
  critical:  'bg-red-900/60 text-red-300',
  worsening: 'bg-orange-900/60 text-orange-300',
  stable:    'bg-yellow-900/60 text-yellow-300',
  improving: 'bg-green-900/60 text-green-300',
  resolved:  'bg-gray-700 text-gray-300',
}

const STATUS_EMOJI = {
  critical: '🔴', worsening: '🟠', stable: '🟡', improving: '🟢', resolved: '✅',
}

function relTime(isoStr) {
  if (!isoStr) return '—'
  const diff = (Date.now() - new Date(isoStr)) / 1000
  if (diff < 60)   return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  return `${Math.round(diff / 3600)}h ago`
}

function MetricCard({ label, value, ci, colorClass = 'text-white' }) {
  const pct = value != null ? `${(value * 100).toFixed(1)}%` : '—'
  const ciStr = ci && ci[0] != null ? `${(ci[0]*100).toFixed(0)}–${(ci[1]*100).toFixed(0)}%` : null
  return (
    <div className="bg-gray-800 rounded-lg p-3 flex flex-col gap-1">
      <span className="text-xs text-gray-400 uppercase tracking-wide">{label}</span>
      <span className={`text-2xl font-bold ${colorClass}`}>{pct}</span>
      {ciStr && <span className="text-xs text-gray-500">95% CI {ciStr}</span>}
    </div>
  )
}

function CountCard({ label, value, color = 'text-white' }) {
  return (
    <div className="bg-gray-800 rounded-lg p-3 flex flex-col gap-1">
      <span className="text-xs text-gray-400 uppercase tracking-wide">{label}</span>
      <span className={`text-xl font-bold ${color}`}>{value ?? '—'}</span>
    </div>
  )
}

// ── Left panel: queue list ────────────────────────────────────────────────────
function QueuePanel({ queue, selectedId, onSelect }) {
  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-gray-700 flex items-center gap-2">
        <span className="text-white font-semibold">Review Queue</span>
        <span className="ml-auto bg-orange-700 text-orange-100 text-xs font-bold px-2 py-0.5 rounded-full">
          {queue.length}
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {queue.length === 0 && (
          <div className="px-4 py-8 text-center text-gray-500 text-sm">
            No alerts pending review
          </div>
        )}
        {queue.map(alert => (
          <button
            key={alert._id}
            onClick={() => onSelect(alert._id)}
            className={`w-full text-left px-4 py-3 border-b border-gray-800 hover:bg-gray-800 transition-colors ${
              selectedId === alert._id ? 'bg-gray-800 border-l-2 border-l-orange-500' : ''
            }`}
          >
            <div className="flex items-center gap-2 mb-1">
              <span className="text-sm">{STATUS_EMOJI[alert.clinical_status] || '🟠'}</span>
              <span className="text-sm text-white font-medium truncate">{alert.problem_name}</span>
            </div>
            <div className="text-xs text-gray-400 truncate">{alert.CPMRN}</div>
            <div className="text-xs text-gray-500 mt-0.5">{relTime(alert.alerted_at)}</div>
          </button>
        ))}
      </div>
    </div>
  )
}

// ── Middle panel: alert detail + verdict ─────────────────────────────────────
function ReviewPanel({ alertId, onVerdictSubmit }) {
  const [alert, setAlert]           = useState(null)
  const [loading, setLoading]       = useState(false)
  const [verdict, setVerdict]       = useState(null)       // "Appropriate" | "Inappropriate"
  const [expl, setExpl]             = useState(null)       // 1 | 2 | 3
  const [notes, setNotes]           = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted]   = useState(false)

  useEffect(() => {
    if (!alertId) return
    setAlert(null); setVerdict(null); setExpl(null); setNotes(''); setSubmitted(false)
    setLoading(true)
    getStudyAlert(alertId)
      .then(data => setAlert(data))
      .catch(err => console.error(err))
      .finally(() => setLoading(false))
  }, [alertId])

  const handleSubmit = useCallback(async () => {
    if (!verdict || !expl) return
    setSubmitting(true)
    try {
      const res = await submitAdjudication(alertId, verdict, expl, notes)
      setSubmitted(true)
      onVerdictSubmit(res.next_alert_id)
    } catch (err) {
      alert('Submit failed: ' + err.message)
    } finally {
      setSubmitting(false)
    }
  }, [alertId, verdict, expl, notes, onVerdictSubmit])

  if (!alertId) return (
    <div className="flex items-center justify-center h-full text-gray-500">
      Select an alert from the queue to review
    </div>
  )
  if (loading) return (
    <div className="flex items-center justify-center h-full text-gray-400">Loading…</div>
  )
  if (!alert) return (
    <div className="flex items-center justify-center h-full text-gray-500">Alert not found</div>
  )

  if (submitted) return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-gray-300">
      <span className="text-4xl">{verdict === 'Appropriate' ? '✅' : '❌'}</span>
      <span className="text-lg font-semibold">Marked {verdict}</span>
      <span className="text-sm text-gray-500">Loading next alert…</span>
    </div>
  )

  return (
    <div className="flex flex-col h-full overflow-y-auto px-6 py-5 gap-5">

      {/* Header */}
      <div className="flex items-start gap-3">
        <span className="text-2xl">{STATUS_EMOJI[alert.clinical_status] || '🟠'}</span>
        <div>
          <h2 className="text-white text-xl font-semibold">{alert.problem_name}</h2>
          <div className="flex items-center gap-3 mt-1 flex-wrap">
            <span className={`text-xs px-2 py-0.5 rounded font-medium ${STATUS_COLOR[alert.clinical_status] || 'bg-gray-700 text-gray-300'}`}>
              {alert.clinical_status}
            </span>
            <span className="text-sm text-gray-400">{alert.CPMRN} · enc {alert.encounter}</span>
            <span className="text-sm text-gray-500">{relTime(alert.alerted_at)}</span>
            {alert.window_open_hours != null && (
              <span className="text-xs text-yellow-500">
                ⏱ {alert.window_open_hours}h window remaining
              </span>
            )}
          </div>
        </div>
        <a
          href={alert.chart_url}
          target="_blank"
          rel="noreferrer"
          className="ml-auto text-xs bg-blue-900/60 text-blue-300 hover:bg-blue-800 px-3 py-1.5 rounded transition-colors whitespace-nowrap"
        >
          Open Chart ↗
        </a>
      </div>

      {/* Why alerting */}
      <section>
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">Why alerting</h3>
        <p className="text-gray-200 text-sm leading-relaxed bg-gray-800/60 rounded-lg px-4 py-3">
          {alert.alert_reason || '—'}
        </p>
      </section>

      {/* Evidence from notes */}
      {alert.cited_notes && alert.cited_notes.length > 0 && (
        <section>
          <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">Evidence from notes</h3>
          <div className="flex flex-col gap-2">
            {alert.cited_notes.slice(0, 3).map((note, i) => (
              <div key={i} className="bg-gray-800/60 rounded-lg px-4 py-3">
                <div className="text-xs text-gray-400 mb-1">
                  📄 {note.note_type || 'Note'} · {note.author || '—'} · {note.timestamp || '—'}
                </div>
                <p className="text-sm text-gray-300 italic">"{note.quote}"</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Suggested actions */}
      {alert.suggestions && alert.suggestions.length > 0 && (
        <section>
          <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">Suggested actions</h3>
          <ul className="text-sm text-gray-300 space-y-1">
            {alert.suggestions.map((s, i) => (
              <li key={i} className="flex gap-2"><span className="text-gray-500">•</span>{s}</li>
            ))}
          </ul>
        </section>
      )}

      {/* Addressed evidence */}
      {alert.addressed_evidence && (
        <section>
          <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">Plan documentation</h3>
          <p className="text-sm text-gray-400 bg-gray-800/40 rounded px-3 py-2 italic">
            {alert.addressed_evidence}
          </p>
        </section>
      )}

      <hr className="border-gray-700" />

      {/* Verdict */}
      <section>
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-3">Verdict</h3>
        <div className="flex gap-3">
          <button
            onClick={() => setVerdict('Appropriate')}
            className={`flex-1 py-2.5 rounded-lg font-semibold text-sm transition-colors border ${
              verdict === 'Appropriate'
                ? 'bg-green-700 border-green-500 text-white'
                : 'bg-gray-800 border-gray-600 text-gray-300 hover:border-green-600'
            }`}
          >
            ✓ Appropriate
          </button>
          <button
            onClick={() => setVerdict('Inappropriate')}
            className={`flex-1 py-2.5 rounded-lg font-semibold text-sm transition-colors border ${
              verdict === 'Inappropriate'
                ? 'bg-red-800 border-red-500 text-white'
                : 'bg-gray-800 border-gray-600 text-gray-300 hover:border-red-600'
            }`}
          >
            ✗ Inappropriate
          </button>
        </div>
      </section>

      {/* Explainability */}
      <section>
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-3">Alert reason clarity</h3>
        <div className="flex gap-2">
          {[
            { val: 1, label: 'Clear & actionable' },
            { val: 2, label: 'Partial'            },
            { val: 3, label: 'Unclear'            },
          ].map(({ val, label }) => (
            <button
              key={val}
              onClick={() => setExpl(val)}
              className={`flex-1 py-2 rounded-lg text-xs font-medium transition-colors border ${
                expl === val
                  ? 'bg-blue-800 border-blue-500 text-white'
                  : 'bg-gray-800 border-gray-700 text-gray-400 hover:border-blue-600'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </section>

      {/* Notes */}
      <section>
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">Notes (optional)</h3>
        <textarea
          value={notes}
          onChange={e => setNotes(e.target.value)}
          rows={2}
          placeholder="Any additional comments…"
          className="w-full bg-gray-800 text-gray-200 text-sm rounded-lg px-3 py-2 border border-gray-700 focus:outline-none focus:border-blue-500 resize-none"
        />
      </section>

      {/* Submit */}
      <button
        onClick={handleSubmit}
        disabled={!verdict || !expl || submitting}
        className="w-full py-3 rounded-lg font-semibold text-sm transition-colors
          disabled:opacity-40 disabled:cursor-not-allowed
          enabled:bg-blue-700 enabled:hover:bg-blue-600 enabled:text-white"
      >
        {submitting ? 'Saving…' : 'Submit & Next'}
      </button>
    </div>
  )
}

// ── Right panel: live metrics ─────────────────────────────────────────────────
function MetricsPanel() {
  const [metrics, setMetrics]   = useState(null)
  const [history, setHistory]   = useState([])
  const [loading, setLoading]   = useState(true)

  const load = useCallback(() => {
    Promise.all([getStudyMetrics(), getStudyMetricsHistory(48)])
      .then(([m, h]) => {
        setMetrics(m.metrics)
        setHistory(h.history.map(s => ({
          t: new Date(s.computed_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
          sens: s.sensitivity != null ? +(s.sensitivity * 100).toFixed(1) : null,
          spec: s.specificity != null ? +(s.specificity * 100).toFixed(1) : null,
          f1:   s.f1          != null ? +(s.f1          * 100).toFixed(1) : null,
        })))
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(); const t = setInterval(load, 60_000); return () => clearInterval(t) }, [load])

  if (loading) return <div className="p-4 text-gray-500 text-sm">Loading metrics…</div>
  if (!metrics) return <div className="p-4 text-gray-500 text-sm">No metrics yet</div>

  return (
    <div className="flex flex-col h-full overflow-y-auto px-4 py-4 gap-4">
      <h2 className="text-white font-semibold text-sm uppercase tracking-wide">Live Metrics</h2>

      {/* Primary metrics */}
      <div className="grid grid-cols-2 gap-2">
        <MetricCard label="Sensitivity" value={metrics.sensitivity} ci={metrics.sensitivity_ci} colorClass="text-green-400" />
        <MetricCard label="Specificity" value={metrics.specificity} ci={metrics.specificity_ci} colorClass="text-blue-400" />
        <MetricCard label="PPV"         value={metrics.ppv}         ci={metrics.ppv_ci}         colorClass="text-yellow-400" />
        <MetricCard label="NPV"         value={metrics.npv}         colorClass="text-purple-400" />
      </div>
      <MetricCard label="F1 Score" value={metrics.f1} colorClass="text-orange-400" />

      {/* 2×2 counts */}
      <div className="grid grid-cols-2 gap-2">
        <CountCard label="TP" value={metrics.tp} color="text-green-400" />
        <CountCard label="FP" value={metrics.fp} color="text-red-400" />
        <CountCard label="FN" value={metrics.fn} color="text-orange-400" />
        <CountCard label="TN" value={metrics.tn} color="text-gray-300" />
      </div>

      {/* Lead time */}
      {metrics.lead_time_median_minutes != null && (
        <div className="bg-gray-800 rounded-lg p-3">
          <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">Median Lead Time</div>
          <div className="text-lg font-bold text-cyan-400">
            {metrics.lead_time_median_minutes > 0
              ? `+${metrics.lead_time_median_minutes} min early`
              : `${Math.abs(metrics.lead_time_median_minutes)} min late`}
          </div>
          {metrics.lead_time_iqr_minutes != null && (
            <div className="text-xs text-gray-500">IQR ±{metrics.lead_time_iqr_minutes} min (n={metrics.lead_time_n})</div>
          )}
        </div>
      )}

      {/* Suppression */}
      {metrics.suppressed_total > 0 && (
        <div className="bg-gray-800 rounded-lg p-3">
          <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">Suppression (addressed)</div>
          <div className="text-sm text-gray-300">
            {metrics.suppressed_with_sbar} / {metrics.suppressed_total} suppressed had a High SBAR
          </div>
          <div className="text-xs text-gray-500 mt-0.5">
            {metrics.suppressed_total > 0
              ? `${((metrics.suppressed_with_sbar / metrics.suppressed_total) * 100).toFixed(0)}% were real events correctly not alerted`
              : ''}
          </div>
        </div>
      )}

      {/* Queue + adjudication */}
      <div className="grid grid-cols-2 gap-2">
        <CountCard label="Pending review" value={metrics.pending_adjudication} color="text-yellow-400" />
        <CountCard label="Adjudicated"    value={metrics.adj_total}            color="text-gray-300" />
      </div>

      {/* Explainability breakdown */}
      {metrics.adj_total > 0 && (
        <div className="bg-gray-800 rounded-lg p-3">
          <div className="text-xs text-gray-400 uppercase tracking-wide mb-2">Explainability</div>
          {[
            { label: 'Clear', val: metrics.expl_clear,   color: 'bg-green-600' },
            { label: 'Partial', val: metrics.expl_partial, color: 'bg-yellow-600' },
            { label: 'Unclear', val: metrics.expl_unclear, color: 'bg-red-700' },
          ].map(({ label, val, color }) => {
            const pct = metrics.adj_total > 0 ? Math.round(val / metrics.adj_total * 100) : 0
            return (
              <div key={label} className="flex items-center gap-2 mb-1">
                <span className="text-xs text-gray-400 w-12">{label}</span>
                <div className="flex-1 bg-gray-700 rounded-full h-1.5">
                  <div className={`${color} h-1.5 rounded-full`} style={{ width: `${pct}%` }} />
                </div>
                <span className="text-xs text-gray-400 w-8 text-right">{pct}%</span>
              </div>
            )
          })}
        </div>
      )}

      {/* Sparkline */}
      {history.length > 1 && (
        <div className="bg-gray-800 rounded-lg p-3">
          <div className="text-xs text-gray-400 uppercase tracking-wide mb-2">Sensitivity over time</div>
          <ResponsiveContainer width="100%" height={80}>
            <LineChart data={history}>
              <XAxis dataKey="t" hide />
              <YAxis domain={[0, 100]} hide />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: 'none', fontSize: 11 }}
                formatter={v => [`${v}%`]}
              />
              <Line type="monotone" dataKey="sens" stroke="#4ade80" dot={false} strokeWidth={2} />
              <Line type="monotone" dataKey="f1"   stroke="#fb923c" dot={false} strokeWidth={1.5} strokeDasharray="3 3" />
            </LineChart>
          </ResponsiveContainer>
          <div className="flex gap-3 mt-1">
            <span className="text-xs text-green-400">— Sensitivity</span>
            <span className="text-xs text-orange-400">--- F1</span>
          </div>
        </div>
      )}

      <div className="text-xs text-gray-600 text-center">
        Updated {metrics.computed_at ? relTime(metrics.computed_at) : '—'}
      </div>
    </div>
  )
}

// ── Cost panel ───────────────────────────────────────────────────────────────
function CostPanel() {
  const [summary, setSummary] = useState(null)
  const [costs, setCosts]     = useState([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    Promise.all([getStudyCostSummary(), getStudyCosts(30)])
      .then(([s, c]) => {
        setSummary(s.summary)
        // Build chart data: last N runs, cost per run
        setCosts(c.costs.map(r => ({
          t:    new Date(r.run_started_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
          cost: r.totals?.cost_usd ?? 0,
          patients: r.patient_count ?? 0,
        })).reverse())  // chronological
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { load(); const t = setInterval(load, 120_000); return () => clearInterval(t) }, [load])

  if (loading) return <div className="p-4 text-gray-500 text-sm">Loading costs…</div>
  if (!summary) return <div className="p-4 text-gray-500 text-sm">No cost data yet. Costs are computed each hourly run.</div>

  const fmtUsd = v => v == null ? '—' : `$${Number(v).toFixed(4)}`

  return (
    <div className="flex flex-col h-full overflow-y-auto px-4 py-4 gap-4">
      <h2 className="text-white font-semibold text-sm uppercase tracking-wide">LLM Cost Tracker</h2>

      {/* Summary cards */}
      <div className="bg-gray-800 rounded-lg p-3 flex flex-col gap-1">
        <span className="text-xs text-gray-400 uppercase tracking-wide">Total spend (all runs)</span>
        <span className="text-2xl font-bold text-yellow-400">{fmtUsd(summary.total_cost_usd)}</span>
        <span className="text-xs text-gray-500">{summary.run_count} run{summary.run_count !== 1 ? 's' : ''}</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <div className="bg-gray-800 rounded-lg p-3 flex flex-col gap-1">
          <span className="text-xs text-gray-400 uppercase tracking-wide">Avg / run</span>
          <span className="text-lg font-bold text-white">{fmtUsd(summary.avg_cost_per_run_usd)}</span>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 flex flex-col gap-1">
          <span className="text-xs text-gray-400 uppercase tracking-wide">Est. / day</span>
          <span className="text-lg font-bold text-cyan-400">{fmtUsd(summary.est_daily_cost_usd)}</span>
        </div>
        <div className="bg-gray-800 rounded-lg p-3 flex flex-col gap-1 col-span-2">
          <span className="text-xs text-gray-400 uppercase tracking-wide">Est. / month (24×30 runs)</span>
          <span className="text-xl font-bold text-orange-400">{fmtUsd(summary.est_monthly_cost_usd)}</span>
        </div>
      </div>

      {/* Token totals */}
      <div className="bg-gray-800 rounded-lg p-3">
        <div className="text-xs text-gray-400 uppercase tracking-wide mb-2">Token totals</div>
        {[
          { label: 'Input', val: summary.total_input_tokens,    color: 'text-blue-400' },
          { label: 'Output', val: summary.total_output_tokens,  color: 'text-green-400' },
          { label: 'Thinking', val: summary.total_thinking_tokens, color: 'text-purple-400' },
        ].map(({ label, val, color }) => (
          <div key={label} className="flex justify-between text-xs mb-1">
            <span className="text-gray-400">{label}</span>
            <span className={color}>{val != null ? val.toLocaleString() : '—'}</span>
          </div>
        ))}
      </div>

      {/* Cost per run sparkline */}
      {costs.length > 1 && (
        <div className="bg-gray-800 rounded-lg p-3">
          <div className="text-xs text-gray-400 uppercase tracking-wide mb-2">Cost per run (USD)</div>
          <ResponsiveContainer width="100%" height={80}>
            <BarChart data={costs}>
              <XAxis dataKey="t" hide />
              <YAxis hide />
              <Tooltip
                contentStyle={{ background: '#1f2937', border: 'none', fontSize: 11 }}
                formatter={(v) => [`$${Number(v).toFixed(4)}`, 'Cost']}
              />
              <Bar dataKey="cost" fill="#f59e0b" radius={[2, 2, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Pricing reference */}
      <div className="bg-gray-800/50 rounded-lg p-3">
        <div className="text-xs text-gray-500 uppercase tracking-wide mb-1">Pricing (gemini-2.5-flash)</div>
        <div className="text-xs text-gray-600 space-y-0.5">
          <div>Input: $0.075 / 1M tokens</div>
          <div>Output: $0.30 / 1M tokens</div>
          <div>Thinking: $3.50 / 1M tokens</div>
        </div>
      </div>
    </div>
  )
}

// ── Main Study page ───────────────────────────────────────────────────────────
export default function Study() {
  const [queue, setQueue]         = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [queueLoading, setQueueLoading] = useState(true)
  const [rightTab, setRightTab]   = useState('metrics')   // 'metrics' | 'costs'

  const loadQueue = useCallback(() => {
    getStudyQueue()
      .then(data => {
        setQueue(data.queue || [])
        if (!selectedId && data.queue?.length > 0) {
          setSelectedId(data.queue[0]._id)
        }
      })
      .catch(console.error)
      .finally(() => setQueueLoading(false))
  }, [selectedId])

  useEffect(() => { loadQueue() }, [])

  const handleVerdictSubmit = useCallback((nextAlertId) => {
    // Remove adjudicated alert from queue
    setQueue(q => q.filter(a => a._id !== selectedId))
    if (nextAlertId) {
      setSelectedId(nextAlertId)
    } else {
      loadQueue()
      setSelectedId(null)
    }
  }, [selectedId, loadQueue])

  return (
    <div className="flex h-full overflow-hidden bg-gray-900">
      {/* Left — queue */}
      <div className="w-64 flex-shrink-0 border-r border-gray-700 flex flex-col">
        {queueLoading
          ? <div className="p-4 text-gray-500 text-sm">Loading queue…</div>
          : <QueuePanel queue={queue} selectedId={selectedId} onSelect={setSelectedId} />
        }
      </div>

      {/* Middle — alert review */}
      <div className="flex-1 flex flex-col border-r border-gray-700 overflow-hidden">
        <ReviewPanel alertId={selectedId} onVerdictSubmit={handleVerdictSubmit} />
      </div>

      {/* Right — metrics / costs */}
      <div className="w-72 flex-shrink-0 flex flex-col">
        {/* Tab bar */}
        <div className="flex border-b border-gray-700 flex-shrink-0">
          {[['metrics', 'Metrics'], ['costs', 'Costs']].map(([id, label]) => (
            <button
              key={id}
              onClick={() => setRightTab(id)}
              className={`flex-1 py-2 text-xs font-medium transition-colors ${
                rightTab === id
                  ? 'text-white border-b-2 border-blue-500'
                  : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="flex-1 overflow-hidden">
          {rightTab === 'metrics' ? <MetricsPanel /> : <CostPanel />}
        </div>
      </div>
    </div>
  )
}
