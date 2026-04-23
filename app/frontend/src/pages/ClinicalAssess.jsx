import { useState, useEffect, useCallback } from 'react'
import {
  ChevronDownIcon,
  ChevronRightIcon,
  PlayIcon,
} from '@heroicons/react/24/outline'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  runClinicalAssessment,
  clinicalAssessJobStatus,
  listClinicalAssessments,
  getClinicalAssessment,
  listAvailablePatients,
  rateSnapshotApi,
} from '../api'
import CostBadge from '../components/CostBadge'
import { useAppState } from '../AppStateContext'

// ── CSV parser ────────────────────────────────────────────────────────────────

function parseCsv(text) {
  const lines = text.trim().split('\n')
  if (lines.length < 2) return { headers: [], rows: [] }
  const parseRow = line => {
    const cells = []
    let cur = '', inQuote = false
    for (let i = 0; i < line.length; i++) {
      const ch = line[i]
      if (ch === '"') { inQuote = !inQuote }
      else if (ch === ',' && !inQuote) { cells.push(cur); cur = '' }
      else { cur += ch }
    }
    cells.push(cur)
    return cells
  }
  const headers = parseRow(lines[0])
  const rows = lines.slice(1).map(parseRow)
  return { headers, rows }
}

const CAT_COLORS = {
  LAB:   'text-blue-400 bg-blue-950/30 border-blue-800/40',
  VITAL: 'text-amber-400 bg-amber-950/30 border-amber-800/40',
  TASK:  'text-purple-400 bg-purple-950/30 border-purple-800/40',
  CHAT:  'text-green-400 bg-green-950/30 border-green-800/40',
}

function TimelineTable({ csv }) {
  const { headers, rows } = parseCsv(csv)
  if (!headers.length) return <pre className="text-xs font-mono text-white/60">{csv}</pre>

  const tsIdx      = headers.findIndex(h => h.toLowerCase().includes('timestamp'))
  const catIdx     = headers.findIndex(h => h.toLowerCase().includes('category'))
  const typeIdx    = headers.findIndex(h => h.toLowerCase().includes('event_type'))
  const summaryIdx = headers.findIndex(h => h.toLowerCase().includes('summary'))

  const fmtTs = raw => {
    try {
      const d = new Date(raw)
      return d.toLocaleString('en-IN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
    } catch { return raw }
  }

  return (
    <div className="overflow-x-auto overflow-y-auto max-h-64 rounded-lg border border-border">
      <table className="min-w-full text-xs">
        <thead className="sticky top-0 bg-ink-800 border-b border-border">
          <tr>
            <th className="px-3 py-2 text-left text-[10px] uppercase tracking-widest text-muted font-medium whitespace-nowrap">Time</th>
            <th className="px-3 py-2 text-left text-[10px] uppercase tracking-widest text-muted font-medium">Cat</th>
            <th className="px-3 py-2 text-left text-[10px] uppercase tracking-widest text-muted font-medium">Type</th>
            <th className="px-3 py-2 text-left text-[10px] uppercase tracking-widest text-muted font-medium w-full">Summary</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/50">
          {rows.map((row, i) => {
            const cat = catIdx >= 0 ? row[catIdx]?.trim().toUpperCase() : ''
            const catCls = CAT_COLORS[cat] || 'text-muted bg-ink-800 border-border'
            return (
              <tr key={i} className="hover:bg-ink-800/40">
                <td className="px-3 py-1.5 text-white/50 font-mono whitespace-nowrap">
                  {tsIdx >= 0 ? fmtTs(row[tsIdx]) : ''}
                </td>
                <td className="px-3 py-1.5">
                  {cat && (
                    <span className={`inline-flex px-1.5 py-0.5 rounded border text-[10px] font-mono ${catCls}`}>
                      {cat}
                    </span>
                  )}
                </td>
                <td className="px-3 py-1.5 text-white/50 whitespace-nowrap">
                  {typeIdx >= 0 ? row[typeIdx]?.trim() : ''}
                </td>
                <td className="px-3 py-1.5 text-white/80 leading-snug">
                  {summaryIdx >= 0 ? row[summaryIdx]?.trim() : row.join(', ')}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ── Badges ────────────────────────────────────────────────────────────────────

const PHASE_COLORS = {
  EVOLVING:      'text-amber-400 bg-amber-950/20 border-amber-800/40',
  ESCALATION:    'text-orange-400 bg-orange-950/20 border-orange-800/40',
  DETERIORATION: 'text-red-400 bg-red-950/20 border-red-800/40',
  MANAGEMENT:    'text-blue-400 bg-blue-950/20 border-blue-800/40',
  LATE:          'text-green-400 bg-green-950/20 border-green-800/40',
}

const DIFFICULTY_COLORS = {
  EASY:   'text-green-400 bg-green-950/20 border-green-800/40',
  MEDIUM: 'text-amber-400 bg-amber-950/20 border-amber-800/40',
  HARD:   'text-red-400 bg-red-950/20 border-red-800/40',
}

function PhaseBadge({ phase }) {
  const cls = PHASE_COLORS[phase?.toUpperCase()] || 'text-muted bg-ink-800 border-border'
  return (
    <span className={`inline-flex px-2 py-0.5 rounded border text-xs font-mono ${cls}`}>
      {phase}
    </span>
  )
}

function DifficultyBadge({ difficulty }) {
  const cls = DIFFICULTY_COLORS[difficulty?.toUpperCase()] || 'text-muted bg-ink-800 border-border'
  return (
    <span className={`inline-flex px-2 py-0.5 rounded border text-xs font-mono ${cls}`}>
      {difficulty}
    </span>
  )
}

// ── Rating widget ─────────────────────────────────────────────────────────────

function RatingWidget({ rating, onRate, saving }) {
  const [hovered, setHovered] = useState(null)
  const active = hovered ?? rating

  return (
    <div className="flex items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-widest text-muted mr-1">Rate</span>
      {Array.from({ length: 10 }, (_, i) => i + 1).map(n => (
        <button
          key={n}
          disabled={saving}
          onClick={() => onRate(n)}
          onMouseEnter={() => setHovered(n)}
          onMouseLeave={() => setHovered(null)}
          className={`w-6 h-6 rounded text-[11px] font-mono border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
            n <= (active ?? 0)
              ? n <= 4  ? 'bg-red-900/60 border-red-700 text-red-300'
              : n <= 7  ? 'bg-amber-900/60 border-amber-700 text-amber-300'
                        : 'bg-green-900/60 border-green-700 text-green-300'
              : 'bg-ink-800 border-border text-muted hover:border-white/30'
          }`}
        >
          {n}
        </button>
      ))}
      {rating != null && (
        <span className="ml-2 text-xs font-mono text-white/60">{rating}/10</span>
      )}
    </div>
  )
}

// ── Patient group + run card (left panel) ─────────────────────────────────────

function fmtRunAt(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleString('en-IN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })
}

function RunCard({ run, isSelected, onSelect }) {
  return (
    <button
      onClick={() => onSelect(run)}
      className={`w-full text-left pl-7 pr-4 py-2 border-b border-border/50 transition-colors ${
        isSelected ? 'bg-accent/10 border-l-2 border-l-accent' : 'hover:bg-ink-800'
      }`}
    >
      <div className="flex items-center gap-2 text-xs text-muted">
        <span className="font-mono text-white/60">{run.run_id}</span>
      </div>
      <div className="flex items-center gap-2 text-[11px] text-muted mt-0.5">
        <span>{fmtRunAt(run.run_at)}</span>
        <span className="text-border">·</span>
        <span>{run.snapshot_count} snaps</span>
      </div>
    </button>
  )
}

function PatientGroup({ patientId, runs, selectedRunId, onSelect }) {
  return (
    <div>
      <div className="px-4 py-2 bg-ink-900 border-b border-border sticky top-0 z-10">
        <p className="text-xs font-mono font-semibold text-white/80">{patientId}</p>
      </div>
      {runs.map(r => (
        <RunCard
          key={r.run_id}
          run={r}
          isSelected={selectedRunId === r.run_id}
          onSelect={onSelect}
        />
      ))}
    </div>
  )
}

// ── Snapshot row ──────────────────────────────────────────────────────────────

function SnapshotRow({ snap, patientId, runId, activeKB, onRated, isOpen, onToggle }) {
  const [saving, setSaving] = useState(false)
  const [localRating, setLocalRating] = useState(snap.rating ?? null)
  const initGaps = Array.isArray(snap.knowledge_gaps)
    ? snap.knowledge_gaps
    : snap.knowledge_gaps ? [snap.knowledge_gaps] : []
  const [gaps, setGaps] = useState(initGaps)
  const [gapInput, setGapInput] = useState('')
  const [gapsSaving, setGapsSaving] = useState(false)
  const hasAnswer = !!snap.agent_answer

  const handleRate = async (n) => {
    setSaving(true)
    try {
      await rateSnapshotApi(patientId, runId, snap.snapshot_num, { rating: n }, activeKB)
      setLocalRating(n)
      onRated?.(snap.snapshot_num, n)
    } catch (err) {
      console.error(err)
    } finally {
      setSaving(false)
    }
  }

  const saveGaps = async (next) => {
    setGapsSaving(true)
    try {
      await rateSnapshotApi(patientId, runId, snap.snapshot_num, { knowledge_gaps: next }, activeKB)
    } catch (err) {
      console.error(err)
    } finally {
      setGapsSaving(false)
    }
  }

  const addGap = () => {
    const val = gapInput.trim()
    if (!val) return
    const next = [...gaps, val]
    setGaps(next)
    setGapInput('')
    saveGaps(next)
  }

  const removeGap = (i) => {
    const next = gaps.filter((_, idx) => idx !== i)
    setGaps(next)
    saveGaps(next)
  }

  const handleGapKeyDown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addGap() }
  }

  return (
    <div className="border border-border rounded-lg overflow-hidden mb-3">
      {/* Header */}
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-4 py-3 bg-ink-900 hover:bg-ink-800 transition-colors text-left"
      >
        <span className="flex-shrink-0 w-6 h-6 rounded-full bg-ink-700 border border-border text-xs font-mono text-muted flex items-center justify-center">
          {snap.snapshot_num}
        </span>
        <div className="flex-1 flex items-center gap-2 flex-wrap">
          <PhaseBadge phase={snap.phase} />
          <DifficultyBadge difficulty={snap.difficulty} />
          {!hasAnswer && (
            <span className="text-xs text-muted italic">Not yet run</span>
          )}
        </div>
        {isOpen
          ? <ChevronDownIcon className="w-4 h-4 text-muted flex-shrink-0" />
          : <ChevronRightIcon className="w-4 h-4 text-muted flex-shrink-0" />
        }
      </button>

      {/* Expanded body */}
      {isOpen && (
        <div className="px-4 py-4 bg-ink-950 border-t border-border">
          {/* Context: timeline + clinical context + question */}
          <div className="mb-5 space-y-3">
            {snap.csv_content && (
              <div>
                <p className="text-[10px] uppercase tracking-widest text-muted mb-1">Timeline</p>
                <TimelineTable csv={snap.csv_content} />
              </div>
            )}
            {snap.clinical_context && (
              <div>
                <p className="text-[10px] uppercase tracking-widest text-muted mb-1">Clinical Context</p>
                <p className="text-xs text-white/70 bg-ink-900 border border-border rounded-lg p-3 leading-relaxed">
                  {snap.clinical_context}
                </p>
              </div>
            )}
            {snap.question && (
              <div>
                <p className="text-[10px] uppercase tracking-widest text-muted mb-1">Question</p>
                <p className="text-xs text-white/80 italic">{snap.question}</p>
              </div>
            )}
          </div>

          {!hasAnswer ? (
            <p className="text-sm text-muted italic">Run the assessment to get an answer.</p>
          ) : (
            <>
              {/* Side-by-side answers */}
              <div className="grid grid-cols-2 gap-4 mb-4">
                {/* Agent answer */}
                <div>
                  <p className="text-xs font-semibold text-accent mb-2 uppercase tracking-wide">
                    Agent Answer
                  </p>
                  <div className="prose-sm text-white/80 text-sm leading-relaxed bg-ink-900 rounded-lg p-3 border border-border">
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>
                      {snap.agent_answer}
                    </ReactMarkdown>
                  </div>
                </div>

                {/* Expected answer */}
                <div>
                  <p className="text-xs font-semibold text-green-400 mb-2 uppercase tracking-wide">
                    Expected Answer
                  </p>
                  <div className="text-sm leading-relaxed bg-ink-900 rounded-lg p-3 border border-border space-y-3">
                    {snap.expected_next_action && (
                      <div>
                        <p className="text-[10px] uppercase tracking-widest text-muted mb-1">
                          Expected Next Action
                        </p>
                        <p className="text-white/80">{snap.expected_next_action}</p>
                      </div>
                    )}
                    {snap.immediate_action && (
                      <div>
                        <p className="text-[10px] uppercase tracking-widest text-muted mb-1">
                          Immediate Action
                        </p>
                        <p className="text-white/80">{snap.immediate_action}</p>
                      </div>
                    )}
                    {snap.clinical_reasoning && (
                      <div>
                        <p className="text-[10px] uppercase tracking-widest text-muted mb-1">
                          Clinical Reasoning
                        </p>
                        <p className="text-white/80 text-xs">{snap.clinical_reasoning}</p>
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Rating + Cost */}
              <div className="pt-3 border-t border-border flex items-center justify-between flex-wrap gap-3">
                <RatingWidget rating={localRating} onRate={handleRate} saving={saving} />
                <CostBadge
                  inputTokens={snap.tokens_in}
                  outputTokens={snap.tokens_out}
                  costUsd={snap.cost_usd}
                />
              </div>

              {/* Knowledge gaps */}
              <div className="pt-3">
                <p className="text-[10px] uppercase tracking-widest text-muted mb-2">
                  Knowledge Gaps
                  {gapsSaving && <span className="ml-2 text-muted/50 normal-case tracking-normal">saving…</span>}
                </p>
                <div className="flex flex-wrap gap-1.5 mb-2">
                  {gaps.map((g, i) => (
                    <span
                      key={i}
                      className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-ink-800 border border-border text-xs text-white/80"
                    >
                      {g}
                      <button
                        onClick={() => removeGap(i)}
                        className="text-muted hover:text-white/80 leading-none"
                        aria-label="Remove"
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
                <div className="flex gap-2">
                  <input
                    value={gapInput}
                    onChange={e => setGapInput(e.target.value)}
                    onKeyDown={handleGapKeyDown}
                    placeholder="Type a gap and press Enter…"
                    className="flex-1 bg-ink-900 border border-border rounded px-3 py-1.5 text-xs text-white/80 placeholder:text-muted/50 focus:outline-none focus:border-accent"
                  />
                  <button
                    onClick={addGap}
                    disabled={!gapInput.trim()}
                    className="px-3 py-1.5 rounded bg-ink-800 border border-border text-xs text-muted hover:text-white hover:border-white/30 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    Add
                  </button>
                </div>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ── Run panel (left panel bottom) ────────────────────────────────────────────

function RunPanel({ activeKB, onRunDone }) {
  const [availablePatients, setAvailablePatients] = useState([])
  const [selectedPatient, setSelectedPatient] = useState('')
  const [running, setRunning] = useState(false)
  const [jobId, setJobId] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    listAvailablePatients()
      .then(data => {
        const patients = data.patients || []
        setAvailablePatients(patients)
        if (patients.length > 0) setSelectedPatient(patients[0])
      })
      .catch(() => {})
  }, [])

  const handleRun = async () => {
    if (!selectedPatient) return
    setRunning(true)
    setError(null)
    try {
      const { job_id } = await runClinicalAssessment(selectedPatient, activeKB)
      setJobId(job_id)
    } catch (err) {
      setError(err.message)
      setRunning(false)
    }
  }

  useEffect(() => {
    if (!jobId) return
    const interval = setInterval(async () => {
      try {
        const job = await clinicalAssessJobStatus(jobId)
        if (job.status === 'done' || job.status === 'error') {
          clearInterval(interval)
          setJobId(null)
          setRunning(false)
          if (job.status === 'error') {
            setError(job.error || 'Assessment failed')
          } else {
            onRunDone()
          }
        }
      } catch {}
    }, 3000)
    return () => clearInterval(interval)
  }, [jobId, onRunDone])

  return (
    <div className="px-4 py-4 border-t border-border">
      <p className="text-xs text-muted mb-2">Run new case assessment</p>
      {availablePatients.length === 0 ? (
        <p className="text-xs text-muted italic mb-2">No patients found in timelines folder.</p>
      ) : (
        <select
          value={selectedPatient}
          onChange={e => setSelectedPatient(e.target.value)}
          disabled={running}
          className="w-full bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white font-mono focus:outline-none focus:border-accent mb-2"
        >
          {availablePatients.map(p => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      )}
      {error && (
        <p className="text-xs text-red-400 mb-2">{error}</p>
      )}
      <button
        onClick={handleRun}
        disabled={running || !selectedPatient}
        className="flex items-center gap-1.5 px-3 py-1.5 bg-accent hover:bg-accent/80 rounded text-xs text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed w-full justify-center"
      >
        <PlayIcon className="w-3.5 h-3.5" />
        {running ? 'Running…' : 'Run Assessment'}
      </button>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function ClinicalAssess() {
  const { activeKB } = useAppState()
  const [assessments, setAssessments] = useState([])
  const [selectedRun, setSelectedRun] = useState(null)  // { patient_id, run_id }
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(false)

  const fetchList = useCallback(async () => {
    try {
      const data = await listClinicalAssessments(activeKB)
      setAssessments(data.assessments || [])
    } catch (err) {
      console.error(err)
    }
  }, [activeKB])

  const fetchDetail = useCallback(async ({ patient_id, run_id }) => {
    setLoading(true)
    try {
      const data = await getClinicalAssessment(patient_id, run_id, activeKB)
      setDetail(data)
    } catch (err) {
      console.error(err)
    } finally {
      setLoading(false)
    }
  }, [activeKB])

  useEffect(() => { fetchList() }, [fetchList])

  useEffect(() => {
    if (selectedRun) fetchDetail(selectedRun)
  }, [selectedRun, fetchDetail])

  const handleRunDone = async () => {
    const data = await listClinicalAssessments(activeKB)
    const list = data.assessments || []
    setAssessments(list)
    // auto-select the newest run
    if (list.length > 0) setSelectedRun({ patient_id: list[0].patient_id, run_id: list[0].run_id })
  }

  const [openSnap, setOpenSnap] = useState(null)
  const [ratings, setRatings] = useState({})

  useEffect(() => {
    if (detail?.snapshots) {
      const initial = {}
      detail.snapshots.forEach(s => { if (s.rating != null) initial[s.snapshot_num] = s.rating })
      setRatings(initial)
      setOpenSnap(null)
    }
  }, [detail])

  const handleRated = (snapNum, rating) => {
    setRatings(prev => ({ ...prev, [snapNum]: rating }))
  }

  const totalCost = detail?.snapshots?.reduce((s, snap) => s + (snap.cost_usd || 0), 0) ?? 0

  const ratedValues = Object.values(ratings)
  const avgRating = ratedValues.length > 0
    ? (ratedValues.reduce((a, b) => a + b, 0) / ratedValues.length).toFixed(1)
    : null

  return (
    <div className="flex h-full">
      {/* Left panel */}
      <div className="w-72 flex-shrink-0 border-r border-border flex flex-col overflow-hidden">
        <div className="px-4 py-4 border-b border-border">
          <h2 className="text-sm font-semibold text-white">
            Clinical Cases
            {assessments.length > 0 && (
              <span className="ml-2 text-xs font-normal text-muted">{assessments.length} runs</span>
            )}
          </h2>
          <p className="text-xs text-muted mt-0.5">Side-by-side agent vs expected</p>
        </div>

        <div className="flex-1 overflow-y-auto">
          {assessments.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-sm text-muted">No cases run yet.</p>
              <p className="text-xs text-muted mt-1">Select a patient below to start.</p>
            </div>
          ) : (() => {
            const groups = assessments.reduce((acc, a) => {
              ;(acc[a.patient_id] = acc[a.patient_id] || []).push(a)
              return acc
            }, {})
            return Object.entries(groups).map(([pid, runs]) => (
              <PatientGroup
                key={pid}
                patientId={pid}
                runs={runs}
                selectedRunId={selectedRun?.run_id}
                onSelect={setSelectedRun}
              />
            ))
          })()}
        </div>

        <RunPanel activeKB={activeKB} onRunDone={handleRunDone} />
      </div>

      {/* Right panel */}
      <div className="flex-1 overflow-y-auto">
        {!selectedRun ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Select a run or start a new assessment
          </div>
        ) : loading ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Loading…
          </div>
        ) : detail ? (
          <div className="px-8 py-6">
            {/* Header */}
            <div className="mb-6">
              <h1 className="text-lg font-semibold text-white font-mono mb-1">
                {detail.patient_id}
              </h1>
              <div className="flex items-center gap-3 text-xs text-muted">
                <span className="font-mono text-white/40">{detail.run_id}</span>
                <span className="text-border">·</span>
                <span>{detail.snapshots?.length ?? 0} snapshots</span>
                {detail.run_at && (
                  <>
                    <span className="text-border">·</span>
                    <span>{fmtRunAt(detail.run_at)}</span>
                  </>
                )}
                {totalCost > 0 && (
                  <>
                    <span className="text-border">·</span>
                    <span className="font-mono">${totalCost.toFixed(4)} total</span>
                  </>
                )}
                {avgRating != null && (
                  <>
                    <span className="text-border">·</span>
                    <span className="font-mono text-amber-400">
                      avg {avgRating}/10
                    </span>
                    <span className="text-muted">({ratedValues.length} rated)</span>
                  </>
                )}
              </div>
            </div>

            {/* Snapshots */}
            <div>
              {detail.snapshots?.map(snap => (
                <SnapshotRow
                  key={snap.snapshot_num}
                  snap={snap}
                  patientId={detail.patient_id}
                  runId={detail.run_id}
                  activeKB={activeKB}
                  onRated={handleRated}
                  isOpen={openSnap === snap.snapshot_num}
                  onToggle={() => setOpenSnap(n => n === snap.snapshot_num ? null : snap.snapshot_num)}
                />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
