import { useState, useEffect, useCallback } from 'react'
import {
  PlayIcon,
  CheckCircleIcon,
  ClockIcon,
  ExclamationCircleIcon,
  ArrowPathIcon,
  StopIcon,
} from '@heroicons/react/24/outline'
import { startLearnRun, learnJobStatus, listLearnRuns, cancelLearnRun, deleteLearnRun, resumeLearnRun, restartLearnRun } from '../api'
import { useAppState } from '../AppStateContext'

// ── Status config ─────────────────────────────────────────────────────────────

const STATUS_CONFIG = {
  running:  { label: 'Running',  classes: 'text-amber-400 bg-amber-950/20 border-amber-800/40',  Icon: ClockIcon },
  complete: { label: 'Complete', classes: 'text-green-400 bg-green-950/20 border-green-800/40',  Icon: CheckCircleIcon },
  error:    { label: 'Error',    classes: 'text-red-400   bg-red-950/20   border-red-800/40',    Icon: ExclamationCircleIcon },
  stopped:  { label: 'Stopped',  classes: 'text-slate-400 bg-slate-900/30 border-slate-700/40',  Icon: StopIcon },
}

function StatusBadge({ status }) {
  const c = STATUS_CONFIG[status] || STATUS_CONFIG.running
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs font-mono ${c.classes}`}>
      <c.Icon className="w-3 h-3" />
      {c.label}
    </span>
  )
}

// ── Phase steps ───────────────────────────────────────────────────────────────

const PHASES = [
  { key: 'exporting',           label: 'Export + Snapshots' },
  { key: 'ingesting',           label: 'Ingest' },
  { key: 'pending_review',      label: 'Review Questions' },
  { key: 'resolving',           label: 'Resolve KGs' },
  { key: 'knowledge_assessing', label: 'Assess' },
  { key: 'complete',            label: 'Done' },
]

const PHASE_ORDER = PHASES.map(p => p.key)

function PhaseBar({ currentPhase, status }) {
  const currentIdx = PHASE_ORDER.indexOf(currentPhase)

  return (
    <div className="flex items-center gap-0 mb-6 overflow-x-auto">
      {PHASES.map(({ key, label }, idx) => {
        const done    = status === 'complete' || idx < currentIdx
        const active  = idx === currentIdx && status !== 'complete'
        const pending = idx > currentIdx && status !== 'complete'
        return (
          <div key={key} className="flex items-center">
            <div className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium whitespace-nowrap ${
              done    ? 'text-green-400 bg-green-950/20'
              : active ? 'text-amber-400 bg-amber-950/20'
              : 'text-muted bg-ink-800'
            }`}>
              {done    && <CheckCircleIcon className="w-3.5 h-3.5" />}
              {active  && <ArrowPathIcon className="w-3.5 h-3.5 animate-spin" />}
              {pending && <span className="w-3.5 h-3.5 rounded-full border border-current inline-block" />}
              {label}
            </div>
            {idx < PHASES.length - 1 && (
              <div className={`h-px w-4 flex-shrink-0 ${done ? 'bg-green-800' : 'bg-border'}`} />
            )}
          </div>
        )
      })}
    </div>
  )
}

// ── Log entry ─────────────────────────────────────────────────────────────────

const PHASE_LABEL = {
  exporting:           'Export',
  ingesting:           'Ingest',
  pending_review:      'Review',
  knowledge_loop:      'Knowledge',
  resolving:           'Resolve',
  knowledge_assessing: 'Assess',
  complete:            'Done',
}

function LogEntry({ entry }) {
  const label = PHASE_LABEL[entry.phase] || entry.phase
  const isError = entry.phase === 'error'
  const hasCost = entry.cost_usd > 0

  return (
    <div className={`border rounded-lg px-4 py-3 mb-2 ${
      isError ? 'border-red-800/40 bg-red-950/10' : 'border-border bg-ink-900'
    }`}>
      <div className="flex items-center justify-between gap-3 mb-1">
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono text-accent px-1.5 py-0.5 bg-accent/10 rounded">
            {label}
            {entry.iteration ? ` · iter ${entry.iteration}` : ''}
          </span>
          {entry.sub_phase && (
            <span className="text-xs text-muted">({entry.sub_phase})</span>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-muted flex-shrink-0">
          {hasCost && (
            <span className="font-mono">${entry.cost_usd.toFixed(4)}</span>
          )}
          <span>{new Date(entry.timestamp).toLocaleTimeString()}</span>
        </div>
      </div>
      <p className={`text-sm ${isError ? 'text-red-300' : 'text-white/80'}`}>
        {entry.message}
      </p>
      {/* Inline stats row */}
      <div className="flex flex-wrap gap-3 mt-1.5 text-xs text-muted">
        {entry.pages_written  != null && <span>{entry.pages_written} pages</span>}
        {entry.kgs_found      != null && <span>{entry.kgs_found} KGs filed</span>}
        {entry.gaps_resolved  != null && <span>{entry.gaps_resolved} gaps resolved</span>}
        {entry.articles_ingested != null && <span>{entry.articles_ingested} articles</span>}
        {entry.new_kgs        != null && <span>{entry.new_kgs} new KGs</span>}
        {entry.assessment_status != null && (
          <span className={entry.assessment_status === 'passing' ? 'text-green-400' : 'text-amber-400'}>
            {entry.assessment_status}
          </span>
        )}
      </div>
    </div>
  )
}

// ── Run card (left panel) ─────────────────────────────────────────────────────

function RunCard({ run, isSelected, onSelect, onDelete }) {
  const [deleting, setDeleting] = useState(false)

  const handleDelete = async (e) => {
    e.stopPropagation()
    setDeleting(true)
    try {
      await onDelete(run.run_id)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div
      onClick={() => onSelect(run.run_id)}
      className={`relative w-full text-left pl-4 pr-8 py-3 border-b border-border transition-colors cursor-pointer group ${
        isSelected ? 'bg-accent/10 border-l-2 border-l-accent' : 'hover:bg-ink-800'
      }`}
    >
      <div className="flex items-center justify-between gap-2 mb-1">
        <p className="text-sm font-mono text-white font-medium truncate">{run.cpmrn}</p>
        <StatusBadge status={run.status} />
      </div>
      <div className="flex items-center gap-2 text-xs text-muted">
        <span>enc {run.encounter}</span>
        {run.started_at && (
          <>
            <span className="text-border">·</span>
            <span>{new Date(run.started_at).toLocaleDateString()}</span>
          </>
        )}
        {run.total_cost_usd > 0 && (
          <>
            <span className="text-border">·</span>
            <span className="font-mono">${run.total_cost_usd.toFixed(4)}</span>
          </>
        )}
      </div>
      <button
        onClick={handleDelete}
        disabled={deleting}
        className="absolute top-2 right-2 w-5 h-5 flex items-center justify-center rounded text-muted hover:text-red-400 hover:bg-red-950/30 opacity-0 group-hover:opacity-100 transition-all disabled:opacity-40 text-xs leading-none"
        title="Delete run"
      >
        ×
      </button>
    </div>
  )
}

// ── Question review panel ─────────────────────────────────────────────────────

function QuestionReviewPanel({ questions, runId, activeKB, onResumed }) {
  const [items, setItems] = useState(questions.map(q => ({ ...q })))
  const [editingId, setEditingId] = useState(null)
  const [editText, setEditText] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const startEdit = (q) => { setEditingId(q.id); setEditText(q.question) }
  const cancelEdit = () => { setEditingId(null); setEditText('') }
  const saveEdit = (id) => {
    setItems(prev => prev.map(q => q.id === id ? { ...q, question: editText.trim() } : q))
    setEditingId(null)
  }

  const handleApprove = async () => {
    setSubmitting(true)
    try {
      await resumeLearnRun(runId, items, activeKB)
      onResumed()
    } catch (err) {
      console.error(err)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="mb-6">
      <div className="flex items-center justify-between mb-3">
        <div>
          <p className="text-sm font-semibold text-white">Review Assessment Questions</p>
          <p className="text-xs text-muted mt-0.5">Edit any question, then approve to continue the learning cycle.</p>
        </div>
        <button
          onClick={handleApprove}
          disabled={submitting}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-green-700 hover:bg-green-600 rounded text-xs text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <CheckCircleIcon className="w-3.5 h-3.5" />
          {submitting ? 'Resuming…' : 'Approve & Continue'}
        </button>
      </div>
      <div className="space-y-2">
        {items.map((q, idx) => (
          <div key={q.id} className="border border-border rounded-lg px-4 py-3 bg-ink-900">
            <div className="flex items-start gap-3">
              <span className="flex-shrink-0 w-5 h-5 rounded-full bg-ink-700 border border-border text-[10px] font-mono text-muted flex items-center justify-center mt-0.5">
                {idx + 1}
              </span>
              <div className="flex-1 min-w-0">
                {editingId === q.id ? (
                  <div className="space-y-2">
                    <textarea
                      value={editText}
                      onChange={e => setEditText(e.target.value)}
                      rows={3}
                      autoFocus
                      className="w-full bg-ink-800 border border-accent rounded px-2 py-1.5 text-xs text-white resize-none focus:outline-none leading-relaxed"
                    />
                    <div className="flex gap-2">
                      <button onClick={() => saveEdit(q.id)} className="text-[10px] px-2 py-1 rounded bg-accent text-white hover:bg-accent/80">Save</button>
                      <button onClick={cancelEdit} className="text-[10px] px-2 py-1 rounded bg-ink-700 text-muted hover:text-white">Cancel</button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-2">
                    <p className="text-xs text-white/85 leading-relaxed">{q.question}</p>
                    <button
                      onClick={() => startEdit(q)}
                      className="flex-shrink-0 text-muted hover:text-white/60 transition-colors mt-0.5"
                      title="Edit question"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                      </svg>
                    </button>
                  </div>
                )}
                {q.rationale && editingId !== q.id && (
                  <p className="text-[10px] text-muted mt-1">{q.rationale}</p>
                )}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Start panel ───────────────────────────────────────────────────────────────

function StartPanel({ activeKB, onStarted }) {
  const [cpmrn, setCpmrn]                 = useState('')
  const [encounter, setEncounter]         = useState('1')
  const [numSnapshots, setNumSnapshots]   = useState('2')
  const [reviewQuestions, setReviewQuestions] = useState(true)
  const [loading, setLoading]             = useState(false)
  const [error, setError]                 = useState(null)

  const handleStart = async () => {
    if (!cpmrn.trim()) return
    setLoading(true)
    setError(null)
    try {
      const { run_id } = await startLearnRun(
        cpmrn.trim(), encounter.trim(), activeKB,
        parseInt(numSnapshots, 10) || 2,
        reviewQuestions,
      )
      onStarted(run_id)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="px-4 py-4 border-t border-border">
      <p className="text-xs text-muted mb-3">Start new learning run</p>
      <div className="space-y-2 mb-3">
        <input
          value={cpmrn}
          onChange={e => setCpmrn(e.target.value)}
          placeholder="CPMRN (e.g. INTSNLG2851387)"
          className="w-full bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white font-mono placeholder:text-muted focus:outline-none focus:border-accent"
        />
        <input
          value={encounter}
          onChange={e => setEncounter(e.target.value)}
          placeholder="Encounter (e.g. 1)"
          className="w-full bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white font-mono placeholder:text-muted focus:outline-none focus:border-accent"
        />
        <div className="flex items-center gap-2">
          <label className="text-xs text-muted whitespace-nowrap">Snapshots</label>
          <input
            type="number" min="1" max="10"
            value={numSnapshots}
            onChange={e => setNumSnapshots(e.target.value)}
            className="w-full bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white font-mono focus:outline-none focus:border-accent"
          />
        </div>
        <button
          onClick={() => setReviewQuestions(v => !v)}
          className={`w-full flex items-center justify-between px-3 py-2 rounded border text-xs font-mono transition-colors ${
            reviewQuestions
              ? 'bg-accent/10 border-accent/40 text-accent'
              : 'bg-ink-800 border-border text-muted'
          }`}
        >
          <span>Review questions after ingest</span>
          <span className={`w-8 h-4 rounded-full relative transition-colors ${reviewQuestions ? 'bg-accent' : 'bg-ink-600'}`}>
            <span className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all ${reviewQuestions ? 'left-4' : 'left-0.5'}`} />
          </span>
        </button>
      </div>
      {error && <p className="text-xs text-red-400 mb-2">{error}</p>}
      <button
        onClick={handleStart}
        disabled={loading || !cpmrn.trim()}
        className="flex items-center justify-center gap-1.5 w-full px-3 py-1.5 bg-accent hover:bg-accent/80 rounded text-xs text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <PlayIcon className="w-3.5 h-3.5" />
        {loading ? 'Starting…' : 'Start Learning'}
      </button>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Learn() {
  const { activeKB }    = useAppState()
  const [runs, setRuns] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail]         = useState(null)

  const fetchList = useCallback(async () => {
    try {
      const data = await listLearnRuns(activeKB)
      setRuns(data.runs || [])
    } catch (err) {
      console.error(err)
    }
  }, [activeKB])

  const fetchDetail = useCallback(async (runId) => {
    try {
      const data = await learnJobStatus(runId, activeKB)
      setDetail(data)
      return data
    } catch (err) {
      console.error(err)
    }
  }, [activeKB])

  useEffect(() => { fetchList() }, [fetchList])

  useEffect(() => {
    if (!selectedId) return
    fetchDetail(selectedId)
  }, [selectedId, fetchDetail])

  // Poll while selected run is running
  useEffect(() => {
    if (!selectedId) return
    const interval = setInterval(async () => {
      const data = await fetchDetail(selectedId)
      if (data?.status === 'running') {
        await fetchList()  // refresh left panel cost / phase
      } else {
        clearInterval(interval)
        await fetchList()
      }
    }, 2000)
    return () => clearInterval(interval)
  }, [selectedId])

  const handleStarted = async (runId) => {
    await fetchList()
    setSelectedId(runId)
    await fetchDetail(runId)
  }

  const handleDelete = async (runId) => {
    try {
      await deleteLearnRun(runId, activeKB)
      if (selectedId === runId) { setSelectedId(null); setDetail(null) }
      await fetchList()
    } catch (err) {
      console.error(err)
    }
  }

  const isRunning = detail?.status === 'running'
  const isStopped = detail?.status === 'stopped' || detail?.status === 'error'
  const [stopping, setStopping] = useState(false)
  const [restarting, setRestarting] = useState(false)

  const handleRestart = async () => {
    if (!detail?.run_id || restarting) return
    setRestarting(true)
    const id = detail.run_id
    try {
      await restartLearnRun(id, activeKB)
      await fetchList()
      // Re-trigger the polling useEffect by cycling selectedId
      setSelectedId(null)
      setDetail(null)
      setTimeout(() => setSelectedId(id), 50)
    } catch (err) {
      console.error(err)
    } finally {
      setRestarting(false)
    }
  }

  const handleStop = async () => {
    if (!detail?.run_id || stopping) return
    setStopping(true)
    try {
      await cancelLearnRun(detail.run_id)
      // keep stopping=true; the poll will clear it once status changes from 'running'
    } catch (err) {
      console.error(err)
      setStopping(false)
    }
  }

  // Clear stopping flag once the run is no longer running
  useEffect(() => {
    if (detail?.status && detail.status !== 'running') {
      setStopping(false)
    }
  }, [detail?.status])

  return (
    <div className="flex h-full">
      {/* Left panel */}
      <div className="w-72 flex-shrink-0 border-r border-border flex flex-col overflow-hidden">
        <div className="px-4 py-4 border-b border-border">
          <h2 className="text-sm font-semibold text-white">
            Learning Runs
            {runs.length > 0 && (
              <span className="ml-2 text-xs font-normal text-muted">{runs.length}</span>
            )}
          </h2>
          <p className="text-xs text-muted mt-0.5">Ingest → resolve → assess → repeat</p>
        </div>

        <div className="flex-1 overflow-y-auto">
          {runs.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-sm text-muted">No runs yet.</p>
              <p className="text-xs text-muted mt-1">Enter a CPMRN below to begin.</p>
            </div>
          ) : (
            runs.map(r => (
              <RunCard
                key={r.run_id}
                run={r}
                isSelected={selectedId === r.run_id}
                onSelect={setSelectedId}
                onDelete={handleDelete}
              />
            ))
          )}
        </div>

        <StartPanel activeKB={activeKB} onStarted={handleStarted} />
      </div>

      {/* Right panel */}
      <div className="flex-1 overflow-y-auto">
        {!selectedId ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Select a run or start a new one
          </div>
        ) : !detail ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Loading…
          </div>
        ) : (
          <div className="px-8 py-6 max-w-3xl">
            {/* Header */}
            <div className="flex items-start justify-between gap-4 mb-4">
              <div>
                <div className="flex items-center gap-3 mb-1">
                  <h1 className="text-lg font-semibold text-white font-mono">
                    {detail.cpmrn}
                  </h1>
                  <span className="text-muted text-sm">/ {detail.encounter}</span>
                  <StatusBadge status={detail.status} />
                </div>
                <div className="flex items-center gap-3 text-xs text-muted">
                  {detail.started_at && (
                    <span>started {new Date(detail.started_at).toLocaleString()}</span>
                  )}
                  {detail.total_cost_usd > 0 && (
                    <>
                      <span className="text-border">·</span>
                      <span className="font-mono">${detail.total_cost_usd.toFixed(4)} total</span>
                    </>
                  )}
                </div>
              </div>
              {isRunning && (
                <div className="flex items-center gap-2 flex-shrink-0">
                  <span className="flex items-center gap-1.5 text-xs text-amber-400">
                    <ArrowPathIcon className="w-3.5 h-3.5 animate-spin" />
                    Running…
                  </span>
                  <button
                    onClick={handleStop}
                    disabled={stopping}
                    className="flex items-center gap-1.5 px-2.5 py-1.5 rounded border border-red-800/60 bg-red-950/30 text-red-400 text-xs hover:bg-red-900/40 hover:border-red-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    <StopIcon className="w-3.5 h-3.5" />
                    {stopping ? 'Stopping…' : 'Stop'}
                  </button>
                </div>
              )}
              {isStopped && (
                <button
                  onClick={handleRestart}
                  disabled={restarting}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 rounded border border-green-800/60 bg-green-950/30 text-green-400 text-xs hover:bg-green-900/40 hover:border-green-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors flex-shrink-0"
                >
                  <ArrowPathIcon className={`w-3.5 h-3.5 ${restarting ? 'animate-spin' : ''}`} />
                  {restarting ? 'Resuming…' : 'Resume'}
                </button>
              )}
            </div>

            {/* Phase bar */}
            <PhaseBar currentPhase={detail.current_phase} status={detail.status} />

            {/* Error */}
            {detail.status === 'error' && detail.error && (
              <div className="mb-4 p-3 bg-red-950/20 border border-red-800/40 rounded-lg text-sm text-red-400">
                {detail.error}
              </div>
            )}

            {/* Question review gate */}
            {detail.current_phase === 'pending_review' && detail.pending_questions?.length > 0 && (
              <QuestionReviewPanel
                questions={detail.pending_questions}
                runId={detail.run_id}
                activeKB={activeKB}
                onResumed={fetchDetail.bind(null, selectedId)}
              />
            )}

            {/* Activity log — newest first */}
            <div>
              <p className="text-xs text-muted mb-3 uppercase tracking-wider">Activity Log</p>
              {detail.log?.length === 0 && (
                <p className="text-sm text-muted italic">Waiting for first log entry…</p>
              )}
              {[...(detail.log || [])].reverse().map((entry, i) => (
                <LogEntry key={i} entry={entry} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
