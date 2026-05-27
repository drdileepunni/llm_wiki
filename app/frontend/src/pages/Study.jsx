import { useEffect, useState, useCallback } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, BarChart, Bar } from 'recharts'
import {
  getStudyQueue, getStudyAlert, submitAdjudication,
  dismissStudyAlert,
  getStudyMetrics, getStudyMetricsHistory,
  getStudyCosts, getStudyCostSummary,
  createStudy,
} from '../api'
import { useAppState } from '../AppStateContext'

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

// Parse any ISO string as UTC — handles both naive strings and +HH:MM offsets
function parseUTC(isoStr) {
  if (!isoStr) return new Date(NaN)
  if (/Z$|[+-]\d{2}:\d{2}$/.test(isoStr.trim())) return new Date(isoStr)
  return new Date(isoStr + 'Z')  // naive string → treat as UTC
}

function relTime(isoStr) {
  if (!isoStr) return '—'
  const diffSec = (Date.now() - parseUTC(isoStr)) / 1000
  if (isNaN(diffSec)) return '—'
  const abs = Math.abs(diffSec)
  const sign = diffSec < 0 ? 'in ' : ''
  const suffix = diffSec < 0 ? '' : ' ago'
  if (abs < 3600) return `${sign}${Math.round(abs / 60)}m${suffix}`
  return `${sign}${Math.round(abs / 3600)}h${suffix}`
}

// Format an ISO timestamp in IST: "2:00 pm IST · 26 May"
function istLabel(isoStr) {
  if (!isoStr) return null
  const d = parseUTC(isoStr)
  if (isNaN(d)) return null
  const time = d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour: 'numeric', minute: '2-digit', hour12: true })
  const date = d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', month: 'short', day: 'numeric' })
  return `${time} IST · ${date}`
}

function istHHMM(isoStr) {
  if (!isoStr) return '—'
  const d = parseUTC(isoStr)
  if (isNaN(d)) return '—'
  return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false })
}

const VITAL_COLS = ['HR', 'SpO2', 'RR', 'BP', 'MAP', 'FiO2', 'GCS', 'GCS-E', 'GCS-V', 'GCS-M']
const VITAL_NORMAL = {
  HR:    v => { const n = parseFloat(v); return n >= 60 && n <= 100 },
  SpO2:  v => { const n = parseFloat(v); return n >= 95 },
  RR:    v => { const n = parseFloat(v); return n >= 12 && n <= 20 },
  MAP:   v => { const n = parseFloat(v); return n >= 65 },
  GCS:   v => { const n = parseFloat(v); return n === 15 },
  'GCS-E': v => { const n = parseFloat(v); return n === 4 },
  'GCS-V': v => { const n = parseFloat(v); return n === 5 },
  'GCS-M': v => { const n = parseFloat(v); return n === 6 },
}

function VitalsTable({ rows }) {
  if (!rows?.length) return (
    <p className="text-xs text-gray-500 italic">No vital readings stored for this window.</p>
  )
  return (
    <div className="overflow-x-auto">
      <table className="text-xs w-full border-collapse">
        <thead>
          <tr>
            <th className="text-left text-gray-500 font-medium pr-3 py-1 whitespace-nowrap">Vital</th>
            {rows.map((r, i) => (
              <th key={i} className="text-center text-gray-500 font-normal px-2 py-1 whitespace-nowrap">
                {istHHMM(r.timestamp)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {VITAL_COLS.map(vital => {
            const hasAny = rows.some(r => r[vital] != null)
            if (!hasAny) return null
            return (
              <tr key={vital} className="border-t border-gray-800">
                <td className="text-gray-400 font-medium pr-3 py-1 whitespace-nowrap">{vital}</td>
                {rows.map((r, i) => {
                  const val = r[vital]
                  const checker = VITAL_NORMAL[vital]
                  const isAbnormal = val != null && checker && !checker(val)
                  return (
                    <td key={i} className={`text-center px-2 py-1 whitespace-nowrap font-mono ${
                      val == null ? 'text-gray-700' : isAbnormal ? 'text-red-400 font-semibold' : 'text-gray-200'
                    }`}>
                      {val ?? '—'}
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function formatNoteText(raw) {
  if (!raw) return ''
  return raw
    .replace(/&nbsp;/g, ' ')       // decode non-breaking spaces
    .split('\n')                    // split on newlines
    .map(line => line.trim())       // trim each line
    .filter(line => line.length)    // drop empty lines
    .map(line => `<p>${line}</p>`)  // wrap each line in a paragraph
    .join('')
}

function LabsSection({ labs }) {
  if (!labs?.length) return (
    <p className="text-xs text-gray-500 italic">No lab results stored for this window.</p>
  )
  return (
    <div className="flex flex-col gap-2">
      {labs.map((panel, i) => (
        <div key={i} className="bg-gray-800/50 rounded px-3 py-2">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs text-gray-300 font-medium">{panel.name}</span>
            <span className="text-xs text-gray-500 ml-auto">{istHHMM(panel.reportedAt)}</span>
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5">
            {panel.values.map((v, j) => (
              <span key={j} className="text-xs text-gray-400">
                <span className="text-gray-300">{v.name}</span>: {v.value} {v.unit}
              </span>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
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
function QueuePanel({ queue, selectedId, onSelect, onDismiss }) {
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
          <div
            key={alert._id}
            className={`relative group border-b border-gray-800 ${
              selectedId === alert._id ? 'bg-gray-800 border-l-2 border-l-orange-500' : ''
            }`}
          >
            <button
              onClick={() => onSelect(alert._id)}
              className="w-full text-left px-4 py-3 pr-8 hover:bg-gray-800 transition-colors"
            >
              <div className="flex items-center gap-2 mb-1">
                <span className="text-sm">{STATUS_EMOJI[alert.clinical_status] || '🟠'}</span>
                <span className="text-sm text-white font-medium truncate">{alert.problem_name}</span>
              </div>
              <div className="text-xs text-gray-400 truncate">{alert.CPMRN}</div>
              <div className="text-xs text-gray-500 mt-0.5">{relTime(alert.alerted_at)}</div>
              {istLabel(alert.alerted_at) && (
                <div className="text-xs text-gray-600 mt-0.5">{istLabel(alert.alerted_at)}</div>
              )}
            </button>
            <button
              onClick={e => { e.stopPropagation(); onDismiss(alert._id) }}
              title="Exclude from study"
              className="absolute top-2 right-2 text-gray-600 hover:text-gray-300 opacity-0 group-hover:opacity-100 transition-opacity text-base leading-none px-1"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Paginated notes ───────────────────────────────────────────────────────────
function NotesCarousel({ notes }) {
  const [idx, setIdx] = useState(0)
  if (!notes?.length) return (
    <p className="text-xs text-gray-500 italic">No clinical notes indexed for this patient.</p>
  )
  const note = notes[idx]
  return (
    <div>
      {/* Pagination bar */}
      <div className="flex items-center gap-2 mb-2">
        <button
          onClick={() => setIdx(i => Math.max(0, i - 1))}
          disabled={idx === 0}
          className="text-gray-500 hover:text-gray-200 disabled:opacity-30 text-sm px-1"
        >‹</button>
        <span className="text-xs text-gray-500">{idx + 1} / {notes.length}</span>
        <button
          onClick={() => setIdx(i => Math.min(notes.length - 1, i + 1))}
          disabled={idx === notes.length - 1}
          className="text-gray-500 hover:text-gray-200 disabled:opacity-30 text-sm px-1"
        >›</button>
        {note.out_of_window && (
          <span className="ml-1 text-xs text-yellow-600">⚠ outside window — best available</span>
        )}
      </div>
      {/* Note card */}
      <div className="bg-gray-800/50 rounded-lg px-4 py-3">
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <span className="text-xs text-gray-300 font-medium">{note.note_type || 'Clinical Note'}</span>
          <span className="text-xs text-gray-500">{note.author || '—'}</span>
          <span className="text-xs text-gray-600 ml-auto">{istHHMM(note.note_time)} IST</span>
        </div>
        <div
          className="note-html-content"
          dangerouslySetInnerHTML={{ __html: formatNoteText(note.text) }}
        />
      </div>
    </div>
  )
}

// ── Alert Window Context accordion ───────────────────────────────────────────
function AlertWindowAccordion({ alert }) {
  const [open, setOpen] = useState(false)

  const windowLabel = alert.window_start
    ? `${istHHMM(alert.window_start)} – ${istHHMM(alert.window_end)} IST`
    : null

  const noteCount = alert.notes_in_window?.length ?? 0

  return (
    <section className="border border-gray-700 rounded-lg">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center w-full text-left px-4 py-3 hover:bg-gray-800/60 transition-colors gap-2 rounded-lg"
      >
        <span className="text-xs text-gray-400 uppercase tracking-wide font-medium">
          Alert window context
        </span>
        {windowLabel && (
          <span className="text-xs text-gray-600 normal-case font-normal">({windowLabel})</span>
        )}
        <span className="ml-auto text-gray-500 text-xs">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className="px-4 pb-4 flex flex-col gap-4 border-t border-gray-700 mt-0">

          {/* Notes — paginated */}
          <div className="pt-4">
            <h4 className="text-xs text-gray-500 uppercase tracking-wide mb-2">
              Notes in window
              {noteCount > 0 && (
                <span className="ml-2 normal-case font-normal text-gray-600">({noteCount})</span>
              )}
            </h4>
            <NotesCarousel notes={alert.notes_in_window} />
          </div>

          {/* Vitals */}
          <div>
            <h4 className="text-xs text-gray-500 uppercase tracking-wide mb-2">Vitals in window</h4>
            <VitalsTable rows={alert.vitals_in_window} />
          </div>

          {/* Labs */}
          <div>
            <h4 className="text-xs text-gray-500 uppercase tracking-wide mb-2">Labs in window</h4>
            <LabsSection labs={alert.labs_in_window} />
          </div>

        </div>
      )}
    </section>
  )
}

// ── Middle panel: alert detail + verdict ─────────────────────────────────────
function NotesAccordion({ notes }) {
  const [open, setOpen] = useState(false)
  return (
    <section>
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-2 text-xs text-gray-400 uppercase tracking-wide mb-2 hover:text-gray-200 transition-colors w-full text-left"
      >
        <span>Evidence from notes ({notes.length})</span>
        <span className="ml-auto">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="flex flex-col gap-2">
          {notes.slice(0, 3).map((note, i) => (
            <div key={i} className="bg-gray-800/60 rounded-lg px-4 py-3">
              <div className="text-xs text-gray-400 mb-1">
                📄 {note.note_type || 'Note'} · {note.author || '—'} · {note.timestamp || '—'}
              </div>
              <p className="text-sm text-gray-300 italic">"{note.quote}"</p>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

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
    if (!verdict) return
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
            <a
              href={`https://cloudphysicianworld.com/patient/${alert.CPMRN}/${alert.encounter}`}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-blue-400 hover:text-blue-300 hover:underline"
            >
              {alert.CPMRN} · enc {alert.encounter}
            </a>
            <span className="text-sm text-gray-500">{relTime(alert.alerted_at)}</span>
            {istLabel(alert.alerted_at) && (
              <span className="text-xs text-gray-600">Run: {istLabel(alert.alerted_at)}</span>
            )}
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

      {/* Evidence from notes — accordion, closed by default */}
      {alert.cited_notes && alert.cited_notes.length > 0 && (
        <NotesAccordion notes={alert.cited_notes} />
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

      {/* Alert Window Context — accordion, closed by default */}
      <AlertWindowAccordion alert={alert} />

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
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-3">Alert reason clarity <span className="text-gray-600 normal-case">(optional)</span></h3>
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
        disabled={!verdict || submitting}
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
function MetricsPanel({ studyId }) {
  const [metrics, setMetrics]   = useState(null)
  const [history, setHistory]   = useState([])
  const [loading, setLoading]   = useState(true)

  const load = useCallback(() => {
    Promise.all([getStudyMetrics(studyId), getStudyMetricsHistory(48)])
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
  }, [studyId])

  useEffect(() => {
    setLoading(true)
    setMetrics(null)
    load()
    const t = setInterval(load, 60_000)
    return () => clearInterval(t)
  }, [load])

  if (loading) return <div className="p-4 text-gray-500 text-sm">Loading metrics…</div>
  if (!metrics) return <div className="p-4 text-gray-500 text-sm">No metrics yet</div>

  return (
    <div className="flex flex-col h-full overflow-y-auto px-4 py-4 gap-4">
      <h2 className="text-white font-semibold text-sm uppercase tracking-wide">Live Metrics</h2>

      {/* Study overview */}
      <div className="bg-gray-800 rounded-lg p-3">
        <div className="text-xs text-gray-400 uppercase tracking-wide mb-1">
          {metrics.study_name || 'Study Overview'}
        </div>
        {metrics.study_start_dt && (
          <div className="text-xs text-gray-600 mb-2">
            {istLabel(metrics.study_start_dt)}
            {metrics.study_end_dt ? ` → ${istLabel(metrics.study_end_dt)}` : ' → ongoing'}
          </div>
        )}
        <div className="flex flex-col gap-1.5">
          {[
            { label: 'Hours in study',        value: metrics.study_hours != null ? `${metrics.study_hours}h` : '—',                  color: 'text-cyan-400' },
            { label: 'Total snapshots',        value: metrics.total_patient_hours ?? '—',                                              color: 'text-white'   },
            { label: 'Total reviewed',         value: metrics.adj_total ?? '—',                                                        color: 'text-green-400'},
            { label: 'Pending review',         value: metrics.pending_adjudication ?? '—',                                             color: 'text-yellow-400'},
            { label: 'Awaiting queue (<2h)',   value: metrics.awaiting_queue ?? '—',                                                   color: 'text-orange-400'},
          ].map(({ label, value, color }) => (
            <div key={label} className="flex justify-between items-center">
              <span className="text-xs text-gray-400">{label}</span>
              <span className={`text-sm font-semibold ${color}`}>{value}</span>
            </div>
          ))}
        </div>
      </div>

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

// ── Study selector bar ────────────────────────────────────────────────────────
function StudySelectorBar({ studies, activeStudy, onSelect, onStudyCreated }) {
  const [showForm, setShowForm] = useState(false)
  const [formName, setFormName]   = useState('')
  const [formStart, setFormStart] = useState('')
  const [formEnd, setFormEnd]     = useState('')
  const [saving, setSaving]       = useState(false)
  const [err, setErr]             = useState(null)

  const handleCreate = async () => {
    if (!formName.trim() || !formStart) { setErr('Name and start date are required.'); return }
    setSaving(true); setErr(null)
    try {
      // datetime-local gives "YYYY-MM-DDTHH:mm" — append UTC offset
      const toISO = s => s ? new Date(s).toISOString() : null
      const res = await createStudy(formName.trim(), toISO(formStart), formEnd ? toISO(formEnd) : null)
      setShowForm(false); setFormName(''); setFormStart(''); setFormEnd('')
      onStudyCreated(res.study)
    } catch (e) {
      setErr(e.message)
    } finally {
      setSaving(false)
    }
  }

  const fmtRange = (s) => {
    if (!s) return ''
    const start = new Date(s.start_dt).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', timeZone: 'Asia/Kolkata' })
    const end   = s.end_dt
      ? new Date(s.end_dt).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', timeZone: 'Asia/Kolkata' })
      : 'ongoing'
    return `${start} – ${end}`
  }

  return (
    <div className="border-b border-gray-700 bg-gray-900 flex-shrink-0">
      <div className="flex items-center gap-2 px-3 py-1.5">
        <span className="text-xs text-gray-500 uppercase tracking-wide whitespace-nowrap">Study</span>
        <select
          value={activeStudy?.study_id || ''}
          onChange={e => {
            const s = studies.find(x => x.study_id === e.target.value)
            onSelect(s || null)
          }}
          className="flex-1 bg-gray-800 text-gray-200 text-xs rounded px-2 py-1 border border-gray-700 focus:outline-none focus:border-blue-500 min-w-0"
        >
          {studies.length === 0 && <option value="">No studies yet</option>}
          {studies.map(s => (
            <option key={s.study_id} value={s.study_id}>
              {s.name} ({fmtRange(s)})
            </option>
          ))}
        </select>
        <button
          onClick={() => setShowForm(f => !f)}
          className="text-xs px-2 py-1 rounded bg-blue-700 hover:bg-blue-600 text-white whitespace-nowrap flex-shrink-0"
        >
          + New
        </button>
      </div>

      {showForm && (
        <div className="px-3 pb-2 flex flex-col gap-1.5">
          <input
            type="text"
            placeholder="Study name"
            value={formName}
            onChange={e => setFormName(e.target.value)}
            className="bg-gray-800 text-gray-200 text-xs rounded px-2 py-1 border border-gray-700 focus:outline-none focus:border-blue-500 w-full"
          />
          <div className="flex gap-1.5">
            <div className="flex-1 flex flex-col gap-0.5">
              <label className="text-xs text-gray-500">Start</label>
              <input
                type="datetime-local"
                value={formStart}
                onChange={e => setFormStart(e.target.value)}
                className="bg-gray-800 text-gray-200 text-xs rounded px-2 py-1 border border-gray-700 focus:outline-none focus:border-blue-500 w-full"
              />
            </div>
            <div className="flex-1 flex flex-col gap-0.5">
              <label className="text-xs text-gray-500">End (optional)</label>
              <input
                type="datetime-local"
                value={formEnd}
                onChange={e => setFormEnd(e.target.value)}
                className="bg-gray-800 text-gray-200 text-xs rounded px-2 py-1 border border-gray-700 focus:outline-none focus:border-blue-500 w-full"
              />
            </div>
          </div>
          {err && <div className="text-xs text-red-400">{err}</div>}
          <div className="flex gap-1.5">
            <button
              onClick={handleCreate}
              disabled={saving}
              className="flex-1 py-1 rounded bg-green-700 hover:bg-green-600 text-white text-xs disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Create Study'}
            </button>
            <button
              onClick={() => { setShowForm(false); setErr(null) }}
              className="flex-1 py-1 rounded bg-gray-700 hover:bg-gray-600 text-gray-300 text-xs"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main Study page ───────────────────────────────────────────────────────────
export default function Study() {
  const [queue, setQueue]             = useState([])
  const [selectedId, setSelectedId]   = useState(null)
  const [queueLoading, setQueueLoading] = useState(true)
  const [rightTab, setRightTab]       = useState('metrics')   // 'metrics' | 'costs'

  const { studies, setStudies, activeStudy, setActiveStudy } = useAppState()

  const handleStudyCreated = useCallback((newStudy) => {
    setStudies(prev => [newStudy, ...prev])
    setActiveStudy(newStudy)
    setQueue([])
    setSelectedId(null)
    setQueueLoading(true)
  }, [setStudies, setActiveStudy])

  const activeStudyId = activeStudy?.study_id || null

  const loadQueue = useCallback(() => {
    getStudyQueue(activeStudyId)
      .then(data => {
        setQueue(data.queue || [])
        if (!selectedId && data.queue?.length > 0) {
          setSelectedId(data.queue[0]._id)
        }
      })
      .catch(console.error)
      .finally(() => setQueueLoading(false))
  }, [selectedId, activeStudyId])

  // Reload queue when active study changes
  useEffect(() => {
    setQueue([])
    setSelectedId(null)
    setQueueLoading(true)
    loadQueue()
  }, [activeStudyId])  // eslint-disable-line react-hooks/exhaustive-deps

  const handleVerdictSubmit = useCallback((nextAlertId) => {
    setQueue(q => q.filter(a => a._id !== selectedId))
    if (nextAlertId) {
      setSelectedId(nextAlertId)
    } else {
      loadQueue()
      setSelectedId(null)
    }
  }, [selectedId, loadQueue])

  const handleDismiss = useCallback(async (alertId) => {
    try {
      const res = await dismissStudyAlert(alertId)
      setQueue(q => q.filter(a => a._id !== alertId))
      if (selectedId === alertId) {
        setSelectedId(res.next_alert_id || null)
      }
    } catch (err) {
      console.error('Dismiss failed:', err)
    }
  }, [selectedId])

  return (
    <div className="flex flex-col h-full overflow-hidden bg-gray-900">
      {/* Study selector bar */}
      <StudySelectorBar
        studies={studies}
        activeStudy={activeStudy}
        onSelect={(s) => { setActiveStudy(s); setQueue([]); setSelectedId(null); setQueueLoading(true) }}
        onStudyCreated={handleStudyCreated}
        showManage={true}
      />

      {/* Three-panel layout */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left — queue */}
        <div className="w-64 flex-shrink-0 border-r border-gray-700 flex flex-col">
          {queueLoading
            ? <div className="p-4 text-gray-500 text-sm">Loading queue…</div>
            : <QueuePanel queue={queue} selectedId={selectedId} onSelect={setSelectedId} onDismiss={handleDismiss} />
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
            {rightTab === 'metrics' ? <MetricsPanel studyId={activeStudyId} /> : <CostPanel />}
          </div>
        </div>
      </div>
    </div>
  )
}
