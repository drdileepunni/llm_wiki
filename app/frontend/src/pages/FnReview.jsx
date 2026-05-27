import { useEffect, useState, useCallback } from 'react'
import { getFnQueue, getFnRecord, submitFnAdjudication, getFnMetrics } from '../api'
import { useAppState } from '../AppStateContext'

function relTime(isoStr) {
  if (!isoStr) return '—'
  const diff = (Date.now() - new Date(isoStr)) / 1000
  if (diff < 60)   return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  return `${Math.round(diff / 3600)}h ago`
}

function istTime(isoStr) {
  if (!isoStr) return '—'
  return new Date(isoStr).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', hour12: true,
  }) + ' IST'
}

function istHHMM(isoStr) {
  if (!isoStr) return '—'
  return new Date(isoStr).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

const VITAL_COLS = ['HR', 'SpO2', 'RR', 'BP', 'MAP', 'FiO2']
const VITAL_NORMAL = {
  HR:   v => { const n = parseFloat(v); return n >= 60 && n <= 100 },
  SpO2: v => { const n = parseFloat(v); return n >= 95 },
  RR:   v => { const n = parseFloat(v); return n >= 12 && n <= 20 },
  MAP:  v => { const n = parseFloat(v); return n >= 65 },
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
                      val == null    ? 'text-gray-700' :
                      isAbnormal     ? 'text-red-400 font-semibold' :
                                       'text-gray-200'
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

const SOURCE_BADGE = {
  sbar: { label: 'SBAR', cls: 'bg-purple-900/60 text-purple-300' },
  task: { label: 'Task', cls: 'bg-blue-900/60 text-blue-300'   },
}

const URGENCY_COLOR = {
  High:   'text-red-400',
  Medium: 'text-yellow-400',
  urgent: 'text-red-400',
  normal: 'text-gray-400',
}

// ── Left panel: FN queue ──────────────────────────────────────────────────────
function FnQueuePanel({ queue, selectedId, onSelect, onQuickExcuse }) {
  return (
    <div className="flex flex-col h-full">
      <div className="px-4 py-3 border-b border-gray-700 flex items-center gap-2">
        <span className="text-white font-semibold">FN Queue</span>
        <span className="ml-auto bg-orange-700 text-orange-100 text-xs font-bold px-2 py-0.5 rounded-full">
          {queue.length}
        </span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {queue.length === 0 && (
          <div className="px-4 py-8 text-center text-gray-500 text-sm">No unreviewed false negatives</div>
        )}
        {queue.map(item => {
          const badge = SOURCE_BADGE[item.source] || SOURCE_BADGE.sbar
          const urgColor = URGENCY_COLOR[item.urgency] || 'text-gray-400'
          return (
            <div
              key={`${item.source}-${item.record_id}`}
              className={`relative group border-b border-gray-800 ${
                selectedId === item.record_id ? 'bg-gray-800 border-l-2 border-l-orange-500' : ''
              }`}
            >
              <button
                onClick={() => onSelect(item.record_id, item.source)}
                className="w-full text-left px-4 py-3 pr-8 hover:bg-gray-800 transition-colors"
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${badge.cls}`}>{badge.label}</span>
                  <span className={`text-xs font-semibold ${urgColor}`}>{item.urgency || '—'}</span>
                </div>
                <div className="text-sm text-gray-200 truncate">{item.issues || '(no description)'}</div>
                <div className="text-xs text-gray-400 mt-0.5 truncate">{item.CPMRN}</div>
                <div className="text-xs text-gray-500">{relTime(item.event_time)}</div>
              </button>
              {/* Quick-excuse button — one click → excused / out of scope */}
              <button
                onClick={e => { e.stopPropagation(); onQuickExcuse(item.record_id, item.source) }}
                title="Excuse as out of scope"
                className="absolute top-2 right-2 text-gray-600 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity text-base leading-none px-1"
              >
                ✕
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Middle panel: FN detail + verdict ─────────────────────────────────────────
const EXCUSAL_REASONS = [
  'Palliative / comfort care',
  'Team already addressing',
  'Transient artefact',
  'Outside system scope',
  'Other',
]

function FnReviewPanel({ recordId, source, onVerdictSubmit }) {
  const [rec, setRec]             = useState(null)
  const [loading, setLoading]     = useState(false)
  const [verdict, setVerdict]     = useState(null)
  const [excusalReason, setExcusalReason] = useState('')
  const [customReason, setCustomReason]   = useState('')
  const [notes, setNotes]         = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)

  useEffect(() => {
    if (!recordId) return
    setRec(null); setVerdict(null); setExcusalReason(''); setCustomReason(''); setNotes(''); setSubmitted(false)
    setLoading(true)
    getFnRecord(recordId, source)
      .then(setRec)
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [recordId, source])

  const handleSubmit = useCallback(async () => {
    if (!verdict) return
    if (verdict === 'excused' && !excusalReason) return
    const reason = excusalReason === 'Other' ? customReason : excusalReason
    setSubmitting(true)
    try {
      const res = await submitFnAdjudication(recordId, source, verdict, reason, notes)
      setSubmitted(true)
      onVerdictSubmit(res.next_record_id, res.next_source)
    } catch (err) {
      alert('Submit failed: ' + err.message)
    } finally {
      setSubmitting(false)
    }
  }, [recordId, source, verdict, excusalReason, customReason, notes, onVerdictSubmit])

  if (!recordId) return (
    <div className="flex items-center justify-center h-full text-gray-500">
      Select a record from the queue to review
    </div>
  )
  if (loading) return <div className="flex items-center justify-center h-full text-gray-400">Loading…</div>
  if (!rec)    return <div className="flex items-center justify-center h-full text-gray-500">Record not found</div>

  if (submitted) return (
    <div className="flex flex-col items-center justify-center h-full gap-3 text-gray-300">
      <span className="text-4xl">
        {verdict === 'true_miss' ? '🎯' : verdict === 'cooldown_miss' ? '⏱' : '✅'}
      </span>
      <span className="text-lg font-semibold capitalize">{verdict.replace('_', ' ')} recorded</span>
      <span className="text-sm text-gray-500">Loading next…</span>
    </div>
  )

  const badge = SOURCE_BADGE[rec.source] || SOURCE_BADGE.sbar

  return (
    <div className="flex flex-col h-full overflow-y-auto px-6 py-5 gap-5">

      {/* Header */}
      <div className="flex items-start gap-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-1 flex-wrap">
            <span className={`text-xs px-2 py-0.5 rounded font-medium ${badge.cls}`}>{badge.label}</span>
            <span className={`text-xs font-semibold ${URGENCY_COLOR[rec.urgency] || 'text-gray-400'}`}>
              {rec.urgency || '—'}
            </span>
            <a
              href={rec.chart_url}
              target="_blank"
              rel="noreferrer"
              className="text-sm text-blue-400 hover:text-blue-300 hover:underline"
            >
              {rec.CPMRN} · enc {rec.encounter}
            </a>
            <span className="text-sm text-gray-500" title={relTime(rec.event_time || rec.create_date_time || rec.task_visible_at)}>
              {istTime(rec.event_time || rec.create_date_time || rec.task_visible_at)}
            </span>
          </div>
          <p className="text-gray-200 font-medium">{rec.hospital_name} — {rec.unit_name}</p>
        </div>
        <a
          href={rec.chart_url}
          target="_blank"
          rel="noreferrer"
          className="text-xs bg-blue-900/60 text-blue-300 hover:bg-blue-800 px-3 py-1.5 rounded transition-colors whitespace-nowrap"
        >
          Open Chart ↗
        </a>
      </div>

      {/* Escalation */}
      <section>
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">Clinical escalation</h3>
        <p className="text-gray-200 text-sm leading-relaxed bg-gray-800/60 rounded-lg px-4 py-3">
          {rec.issues || '(no description)'}
        </p>
      </section>

      {/* Cooldown hint */}
      {rec.cooldown_hint && (
        <div className="flex items-start gap-2 bg-yellow-900/30 border border-yellow-700/50 rounded-lg px-4 py-3">
          <span className="text-yellow-400 text-base">⏱</span>
          <div>
            <p className="text-yellow-300 text-sm font-medium">Cooldown suppression detected</p>
            <p className="text-yellow-400/70 text-xs mt-0.5">
              {rec.suppressed_events.length} suppressed event{rec.suppressed_events.length !== 1 ? 's' : ''} found in
              the match window — the system detected this problem but the 8h cooldown blocked the alert.
              Consider <strong>Cooldown Miss</strong> verdict.
            </p>
          </div>
        </div>
      )}

      {/* What the system knew */}
      {rec.patient_problems_snapshot?.length > 0 && (
        <section>
          <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">System knowledge at event time</h3>
          <div className="flex flex-col gap-1.5">
            {rec.patient_problems_snapshot.map((p, i) => (
              <div key={i} className="flex items-center gap-2 bg-gray-800/50 rounded px-3 py-2">
                <span className="text-xs text-gray-300 font-medium flex-1">{p.problem_name}</span>
                <span className={`text-xs px-1.5 py-0.5 rounded ${
                  p.clinical_status === 'critical'  ? 'bg-red-900/60 text-red-300' :
                  p.clinical_status === 'worsening' ? 'bg-orange-900/60 text-orange-300' :
                  p.clinical_status === 'stable'    ? 'bg-yellow-900/60 text-yellow-300' :
                  'bg-gray-700 text-gray-300'
                }`}>{p.clinical_status}</span>
                {p.being_addressed && (
                  <span className="text-xs text-green-400">addressed</span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Nearby system alerts */}
      {rec.nearby_alerts?.length > 0 && (
        <section>
          <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">
            System alerts in window ({rec.nearby_alerts.length})
          </h3>
          <div className="flex flex-col gap-1.5">
            {rec.nearby_alerts.map((a, i) => (
              <div key={i} className="bg-gray-800/50 rounded px-3 py-2">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-300 font-medium">{a.problem_name}</span>
                  <span className="text-xs text-gray-500" title={relTime(a.alerted_at)}>{istTime(a.alerted_at)}</span>
                  <span className="text-xs text-gray-600 ml-auto">{a.match_status}</span>
                </div>
                {a.alert_reason && (
                  <p className="text-xs text-gray-500 mt-0.5 truncate">{a.alert_reason}</p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Vitals in window */}
      <section>
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">
          Vitals in window
          {rec.window_start && (
            <span className="ml-2 normal-case font-normal text-gray-600">
              {istHHMM(rec.window_start)} – {istHHMM(rec.window_end)} IST
            </span>
          )}
        </h3>
        <VitalsTable rows={rec.vitals_in_window} />
      </section>

      {/* Labs in window */}
      <section>
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">
          Labs in window
        </h3>
        <LabsSection labs={rec.labs_in_window} />
      </section>

      <hr className="border-gray-700" />

      {/* Verdict */}
      <section>
        <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-3">Verdict</h3>
        <div className="flex flex-col gap-2">
          {[
            { v: 'true_miss',     label: '🎯 True Miss',      desc: 'System should have detected this and didn\'t',         cls: 'border-red-600 bg-red-900/30 text-red-200' },
            { v: 'cooldown_miss', label: '⏱ Cooldown Miss',   desc: 'System detected it but cooldown blocked the alert',    cls: 'border-yellow-600 bg-yellow-900/30 text-yellow-200' },
            { v: 'excused',       label: '✅ Excused',         desc: 'Valid reason system was silent — remove from FN count', cls: 'border-green-600 bg-green-900/30 text-green-200' },
          ].map(({ v, label, desc, cls }) => (
            <button
              key={v}
              onClick={() => setVerdict(v)}
              className={`text-left px-4 py-3 rounded-lg border transition-colors ${
                verdict === v ? cls : 'border-gray-700 bg-gray-800/60 text-gray-400 hover:border-gray-500'
              }`}
            >
              <div className="font-semibold text-sm">{label}</div>
              <div className="text-xs mt-0.5 opacity-70">{desc}</div>
            </button>
          ))}
        </div>
      </section>

      {/* Excusal reason — only when Excused selected */}
      {verdict === 'excused' && (
        <section>
          <h3 className="text-xs text-gray-400 uppercase tracking-wide mb-2">Reason for excusal</h3>
          <div className="flex flex-col gap-1.5">
            {EXCUSAL_REASONS.map(r => (
              <button
                key={r}
                onClick={() => setExcusalReason(r)}
                className={`text-left text-sm px-3 py-2 rounded-lg border transition-colors ${
                  excusalReason === r
                    ? 'border-green-600 bg-green-900/30 text-green-200'
                    : 'border-gray-700 text-gray-400 hover:border-gray-500'
                }`}
              >
                {r}
              </button>
            ))}
          </div>
          {excusalReason === 'Other' && (
            <input
              type="text"
              value={customReason}
              onChange={e => setCustomReason(e.target.value)}
              placeholder="Describe reason…"
              className="mt-2 w-full bg-gray-800 text-gray-200 text-sm rounded-lg px-3 py-2 border border-gray-700 focus:outline-none focus:border-green-500"
            />
          )}
        </section>
      )}

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
        disabled={!verdict || submitting || (verdict === 'excused' && !excusalReason) || (verdict === 'excused' && excusalReason === 'Other' && !customReason)}
        className="w-full py-3 rounded-lg font-semibold text-sm transition-colors
          disabled:opacity-40 disabled:cursor-not-allowed
          enabled:bg-orange-700 enabled:hover:bg-orange-600 enabled:text-white"
      >
        {submitting ? 'Saving…' : 'Submit & Next'}
      </button>
    </div>
  )
}

// ── Right panel: FN metrics ───────────────────────────────────────────────────
function FnMetricsPanel({ studyId }) {
  const [m, setM]         = useState(null)
  const [loading, setL]   = useState(true)

  const load = useCallback(() => {
    getFnMetrics(studyId)
      .then(d => setM(d))
      .catch(console.error)
      .finally(() => setL(false))
  }, [studyId])

  useEffect(() => {
    setL(true); setM(null)
    load()
    const t = setInterval(load, 30_000)
    return () => clearInterval(t)
  }, [load])

  if (loading) return <div className="p-4 text-gray-500 text-sm">Loading…</div>
  if (!m)      return <div className="p-4 text-gray-500 text-sm">No data</div>

  const total = m.fn_unreviewed + m.fn_true_miss + m.fn_cooldown_miss + m.fn_excused
  const bar = (val) => total > 0 ? Math.round(val / total * 100) : 0

  return (
    <div className="flex flex-col h-full overflow-y-auto px-4 py-4 gap-4">
      <h2 className="text-white font-semibold text-sm uppercase tracking-wide">FN Breakdown</h2>

      {/* Summary */}
      <div className="bg-gray-800 rounded-lg p-3">
        <div className="text-xs text-gray-400 uppercase tracking-wide mb-2">Total in FN count</div>
        <div className="text-3xl font-bold text-orange-400">{m.fn_total_in_count}</div>
        <div className="text-xs text-gray-500 mt-1">{m.fn_total_adjudicated} reviewed · {m.fn_unreviewed} pending</div>
      </div>

      {/* Breakdown bars */}
      <div className="bg-gray-800 rounded-lg p-3">
        <div className="text-xs text-gray-400 uppercase tracking-wide mb-3">Review breakdown</div>
        {[
          { label: 'Unreviewed',    val: m.fn_unreviewed,    color: 'bg-gray-500' },
          { label: 'True Miss',     val: m.fn_true_miss,     color: 'bg-red-600'  },
          { label: 'Cooldown Miss', val: m.fn_cooldown_miss, color: 'bg-yellow-600' },
          { label: 'Excused',       val: m.fn_excused,       color: 'bg-green-600' },
        ].map(({ label, val, color }) => (
          <div key={label} className="flex items-center gap-2 mb-2">
            <span className="text-xs text-gray-400 w-28 shrink-0">{label}</span>
            <div className="flex-1 bg-gray-700 rounded-full h-1.5">
              <div className={`${color} h-1.5 rounded-full transition-all`} style={{ width: `${bar(val)}%` }} />
            </div>
            <span className="text-xs text-gray-300 w-6 text-right">{val}</span>
          </div>
        ))}
      </div>

      {/* What excused means for sensitivity */}
      <div className="bg-gray-800/50 rounded-lg p-3">
        <div className="text-xs text-gray-500 uppercase tracking-wide mb-1">Impact</div>
        <div className="text-xs text-gray-400 leading-relaxed">
          <span className="text-green-400">{m.fn_excused}</span> excused FNs removed from count.
          True Miss + Cooldown Miss = <span className="text-orange-400">{m.fn_true_miss + m.fn_cooldown_miss}</span> confirmed system gaps.
        </div>
      </div>

      <div className="text-xs text-gray-600 text-center">Auto-refreshes every 30s</div>
    </div>
  )
}

// ── Main FnReview page ────────────────────────────────────────────────────────
export default function FnReview() {
  const [queue, setQueue]       = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [selectedSource, setSelectedSource] = useState('sbar')
  const [queueLoading, setQueueLoading] = useState(true)

  const { activeStudy } = useAppState()
  const activeStudyId = activeStudy?.study_id || null

  const loadQueue = useCallback(() => {
    getFnQueue(activeStudyId)
      .then(d => {
        setQueue(d.queue || [])
        if (!selectedId && d.queue?.length > 0) {
          setSelectedId(d.queue[0].record_id)
          setSelectedSource(d.queue[0].source)
        }
      })
      .catch(console.error)
      .finally(() => setQueueLoading(false))
  }, [selectedId, activeStudyId])

  useEffect(() => {
    setQueue([]); setSelectedId(null); setQueueLoading(true)
    loadQueue()
  }, [activeStudyId])  // eslint-disable-line react-hooks/exhaustive-deps

  const handleVerdictSubmit = useCallback((nextRecordId, nextSource) => {
    setQueue(q => q.filter(i => i.record_id !== selectedId))
    if (nextRecordId) {
      setSelectedId(nextRecordId)
      setSelectedSource(nextSource || 'sbar')
    } else {
      loadQueue()
      setSelectedId(null)
    }
  }, [selectedId, loadQueue])

  const handleSelect = useCallback((recordId, source) => {
    setSelectedId(recordId)
    setSelectedSource(source)
  }, [])

  const handleQuickExcuse = useCallback(async (recordId, source) => {
    try {
      await submitFnAdjudication(recordId, source, 'excused', 'Out of scope', 'Quick-excused from queue')
      setQueue(q => q.filter(i => i.record_id !== recordId))
      if (selectedId === recordId) setSelectedId(null)
    } catch (err) {
      console.error('Quick excuse failed:', err)
    }
  }, [selectedId])

  return (
    <div className="flex h-full overflow-hidden bg-gray-900">
      {/* Left — queue */}
      <div className="w-64 flex-shrink-0 border-r border-gray-700 flex flex-col">
        {queueLoading
          ? <div className="p-4 text-gray-500 text-sm">Loading queue…</div>
          : <FnQueuePanel queue={queue} selectedId={selectedId} onSelect={handleSelect} onQuickExcuse={handleQuickExcuse} />
        }
      </div>

      {/* Middle — detail + verdict */}
      <div className="flex-1 flex flex-col border-r border-gray-700 overflow-hidden">
        <div className="px-6 py-3 border-b border-gray-700 flex-shrink-0 flex items-center gap-3">
          <span className="text-white font-semibold">False Negative Review</span>
          <span className="text-xs text-gray-500">Review unmatched escalations and classify each miss</span>
          {activeStudy && (
            <span className="ml-auto text-xs text-blue-400 bg-blue-900/30 px-2 py-0.5 rounded">
              {activeStudy.name}
            </span>
          )}
        </div>
        <div className="flex-1 overflow-hidden">
          <FnReviewPanel
            recordId={selectedId}
            source={selectedSource}
            onVerdictSubmit={handleVerdictSubmit}
          />
        </div>
      </div>

      {/* Right — FN metrics */}
      <div className="w-64 flex-shrink-0 border-l border-gray-700">
        <FnMetricsPanel studyId={activeStudyId} />
      </div>
    </div>
  )
}
