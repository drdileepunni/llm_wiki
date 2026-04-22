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
} from '../api'
import CostBadge from '../components/CostBadge'
import { useAppState } from '../AppStateContext'

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

// ── Patient card (left panel) ─────────────────────────────────────────────────

function PatientCard({ assessment, isSelected, onSelect }) {
  return (
    <button
      onClick={() => onSelect(assessment.patient_id)}
      className={`w-full text-left px-4 py-3 border-b border-border transition-colors ${
        isSelected ? 'bg-accent/10 border-l-2 border-l-accent' : 'hover:bg-ink-800'
      }`}
    >
      <p className="text-sm text-white font-mono font-medium mb-1">{assessment.patient_id}</p>
      <div className="flex items-center gap-2 text-xs text-muted">
        <span>{assessment.snapshot_count} snapshots</span>
        {assessment.run_at && (
          <>
            <span className="text-border">·</span>
            <span>{new Date(assessment.run_at).toLocaleDateString()}</span>
          </>
        )}
      </div>
    </button>
  )
}

// ── Snapshot row ──────────────────────────────────────────────────────────────

function SnapshotRow({ snap }) {
  const [expanded, setExpanded] = useState(false)
  const hasAnswer = !!snap.agent_answer

  return (
    <div className="border border-border rounded-lg overflow-hidden mb-3">
      {/* Header */}
      <button
        onClick={() => setExpanded(v => !v)}
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
        {expanded
          ? <ChevronDownIcon className="w-4 h-4 text-muted flex-shrink-0" />
          : <ChevronRightIcon className="w-4 h-4 text-muted flex-shrink-0" />
        }
      </button>

      {/* Expanded body */}
      {expanded && (
        <div className="px-4 py-4 bg-ink-950 border-t border-border">
          {/* Question */}
          <p className="text-xs text-muted mb-4 italic">{snap.question}</p>

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

              {/* Cost */}
              <div className="pt-3 border-t border-border">
                <CostBadge
                  inputTokens={snap.tokens_in}
                  outputTokens={snap.tokens_out}
                  costUsd={snap.cost_usd}
                />
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
  const [patientDir, setPatientDir] = useState('')
  const [running, setRunning] = useState(false)
  const [jobId, setJobId] = useState(null)
  const [error, setError] = useState(null)

  const handleRun = async () => {
    if (!patientDir.trim()) return
    setRunning(true)
    setError(null)
    try {
      const { job_id } = await runClinicalAssessment(patientDir.trim(), activeKB)
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
      <textarea
        value={patientDir}
        onChange={e => setPatientDir(e.target.value)}
        placeholder="/path/to/patient/INASDBR459102"
        rows={2}
        className="w-full bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white font-mono placeholder:text-muted focus:outline-none focus:border-accent resize-none mb-2"
      />
      {error && (
        <p className="text-xs text-red-400 mb-2">{error}</p>
      )}
      <button
        onClick={handleRun}
        disabled={running || !patientDir.trim()}
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
  const [selectedId, setSelectedId] = useState(null)
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

  const fetchDetail = useCallback(async (id) => {
    setLoading(true)
    try {
      const data = await getClinicalAssessment(id, activeKB)
      setDetail(data)
    } catch (err) {
      console.error(err)
    } finally {
      setLoading(false)
    }
  }, [activeKB])

  useEffect(() => { fetchList() }, [fetchList])

  useEffect(() => {
    if (selectedId) fetchDetail(selectedId)
  }, [selectedId, fetchDetail])

  const handleRunDone = async () => {
    await fetchList()
    if (selectedId) await fetchDetail(selectedId)
    // auto-select if first run
    if (!selectedId) {
      const data = await listClinicalAssessments(activeKB)
      const list = data.assessments || []
      if (list.length > 0) setSelectedId(list[list.length - 1].patient_id)
    }
  }

  const totalCost = detail?.snapshots?.reduce((s, snap) => s + (snap.cost_usd || 0), 0) ?? 0

  return (
    <div className="flex h-full">
      {/* Left panel */}
      <div className="w-72 flex-shrink-0 border-r border-border flex flex-col overflow-hidden">
        <div className="px-4 py-4 border-b border-border">
          <h2 className="text-sm font-semibold text-white">
            Clinical Cases
            {assessments.length > 0 && (
              <span className="ml-2 text-xs font-normal text-muted">{assessments.length}</span>
            )}
          </h2>
          <p className="text-xs text-muted mt-0.5">Side-by-side agent vs expected</p>
        </div>

        <div className="flex-1 overflow-y-auto">
          {assessments.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-sm text-muted">No cases run yet.</p>
              <p className="text-xs text-muted mt-1">Enter a patient dir below to start.</p>
            </div>
          ) : (
            assessments.map(a => (
              <PatientCard
                key={a.patient_id}
                assessment={a}
                isSelected={selectedId === a.patient_id}
                onSelect={setSelectedId}
              />
            ))
          )}
        </div>

        <RunPanel activeKB={activeKB} onRunDone={handleRunDone} />
      </div>

      {/* Right panel */}
      <div className="flex-1 overflow-y-auto">
        {!selectedId ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Select a case or run a new assessment
          </div>
        ) : loading ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Loading…
          </div>
        ) : detail ? (
          <div className="px-8 py-6 max-w-5xl">
            {/* Header */}
            <div className="mb-6">
              <h1 className="text-lg font-semibold text-white font-mono mb-1">
                {detail.patient_id}
              </h1>
              <div className="flex items-center gap-3 text-xs text-muted">
                <span>{detail.snapshots?.length ?? 0} snapshots</span>
                {detail.run_at && (
                  <>
                    <span className="text-border">·</span>
                    <span>run {new Date(detail.run_at).toLocaleString()}</span>
                  </>
                )}
                {totalCost > 0 && (
                  <>
                    <span className="text-border">·</span>
                    <span className="font-mono">${totalCost.toFixed(4)} total</span>
                  </>
                )}
              </div>
            </div>

            {/* Snapshots */}
            <div>
              {detail.snapshots?.map(snap => (
                <SnapshotRow key={snap.snapshot_num} snap={snap} />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
