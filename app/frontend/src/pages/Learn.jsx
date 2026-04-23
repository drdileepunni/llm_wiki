import { useState, useEffect, useCallback } from 'react'
import {
  PlayIcon,
  CheckCircleIcon,
  ClockIcon,
  ExclamationCircleIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline'
import { startLearnRun, learnJobStatus, listLearnRuns } from '../api'
import { useAppState } from '../AppStateContext'

// ── Status config ─────────────────────────────────────────────────────────────

const STATUS_CONFIG = {
  running:  { label: 'Running',  classes: 'text-amber-400 bg-amber-950/20 border-amber-800/40',  Icon: ClockIcon },
  complete: { label: 'Complete', classes: 'text-green-400 bg-green-950/20 border-green-800/40',  Icon: CheckCircleIcon },
  error:    { label: 'Error',    classes: 'text-red-400   bg-red-950/20   border-red-800/40',    Icon: ExclamationCircleIcon },
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
  { key: 'ingesting',           label: 'Ingest' },
  { key: 'resolving',           label: 'Resolve KGs' },
  { key: 'knowledge_assessing', label: 'Assess' },
  { key: 'clinical_assessing',  label: 'Clinical' },
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
  ingesting:           'Ingest',
  knowledge_loop:      'Knowledge',
  clinical_loop:       'Clinical',
  resolving:           'Resolve',
  knowledge_assessing: 'Assess',
  clinical_assessing:  'Clinical',
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

function RunCard({ run, isSelected, onSelect }) {
  return (
    <button
      onClick={() => onSelect(run.run_id)}
      className={`w-full text-left px-4 py-3 border-b border-border transition-colors ${
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
    </button>
  )
}

// ── Start panel ───────────────────────────────────────────────────────────────

function StartPanel({ activeKB, onStarted }) {
  const [cpmrn, setCpmrn]       = useState('')
  const [encounter, setEncounter] = useState('1')
  const [loading, setLoading]    = useState(false)
  const [error, setError]        = useState(null)

  const handleStart = async () => {
    if (!cpmrn.trim()) return
    setLoading(true)
    setError(null)
    try {
      const { run_id } = await startLearnRun(cpmrn.trim(), encounter.trim(), activeKB)
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

  const isRunning = detail?.status === 'running'

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
                <span className="flex items-center gap-1.5 text-xs text-amber-400 flex-shrink-0">
                  <ArrowPathIcon className="w-3.5 h-3.5 animate-spin" />
                  Polling every 5s…
                </span>
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
