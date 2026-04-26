import { useState, useEffect, useCallback } from 'react'
import {
  ChevronDownIcon,
  ChevronRightIcon,
  PlayIcon,
  HandThumbUpIcon,
  HandThumbDownIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  ClockIcon,
  PencilIcon,
} from '@heroicons/react/24/outline'
import {
  HandThumbUpIcon as HandThumbUpSolid,
  HandThumbDownIcon as HandThumbDownSolid,
} from '@heroicons/react/24/solid'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { listAssessments, getAssessment, runAssessment, rateQuestion, assessJobStatus, updateQuestion } from '../api'
import CostBadge from '../components/CostBadge'
import { useAppState } from '../AppStateContext'

// ── Status badge ──────────────────────────────────────────────────────────────

function StatusBadge({ status }) {
  const config = {
    pending:     { label: 'Pending',     classes: 'text-muted bg-ink-800 border-border' },
    in_progress: { label: 'In Progress', classes: 'text-amber-400 bg-amber-950/20 border-amber-800/40' },
    passing:     { label: 'Passing',     classes: 'text-green-400 bg-green-950/20 border-green-800/40' },
  }
  const c = config[status] || config.pending
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded border text-xs font-mono ${c.classes}`}>
      {status === 'passing' && <CheckCircleIcon className="w-3 h-3" />}
      {status === 'in_progress' && <ClockIcon className="w-3 h-3" />}
      {c.label}
    </span>
  )
}

// ── Assessment card (left panel) ──────────────────────────────────────────────

function AssessmentCard({ assessment, isSelected, onSelect, onRunDone, activeKB }) {
  const [running, setRunning] = useState(false)
  const [jobId, setJobId] = useState(null)

  const handleRun = async (e) => {
    e.stopPropagation()
    setRunning(true)
    try {
      const { job_id } = await runAssessment(assessment.source_slug, activeKB)
      setJobId(job_id)
    } catch (err) {
      console.error(err)
      setRunning(false)
    }
  }

  useEffect(() => {
    if (!jobId) return
    const interval = setInterval(async () => {
      try {
        const job = await assessJobStatus(jobId)
        if (job.status === 'done' || job.status === 'error') {
          clearInterval(interval)
          setJobId(null)
          setRunning(false)
          if (job.status === 'done') onRunDone(assessment.source_slug)
        }
      } catch {}
    }, 3000)
    return () => clearInterval(interval)
  }, [jobId])

  const hasNewKGs = assessment.latest_new_kgs > 0

  return (
    <button
      onClick={() => onSelect(assessment.source_slug)}
      className={`w-full text-left px-4 py-3 border-b border-border transition-colors ${
        isSelected ? 'bg-accent/10 border-l-2 border-l-accent' : 'hover:bg-ink-800'
      }`}
    >
      <div className="flex items-start justify-between gap-2 mb-1.5">
        <p className="text-sm text-white font-medium leading-snug line-clamp-2 flex-1">
          {assessment.title}
        </p>
        <StatusBadge status={assessment.status} />
      </div>

      <div className="flex items-center gap-3 text-xs text-muted mb-2">
        <span>{assessment.question_count} questions</span>
        {assessment.last_run && (
          <>
            <span className="text-border">·</span>
            <span>last run {new Date(assessment.last_run).toLocaleDateString()}</span>
          </>
        )}
      </div>

      {hasNewKGs && (
        <div className="flex items-center gap-1.5 text-xs text-amber-400 mb-2">
          <ExclamationTriangleIcon className="w-3.5 h-3.5 flex-shrink-0" />
          <span>{assessment.latest_new_kgs} new KG{assessment.latest_new_kgs !== 1 ? 's' : ''} in latest run</span>
        </div>
      )}

      <button
        onClick={handleRun}
        disabled={running}
        className="flex items-center gap-1.5 px-2.5 py-1 bg-ink-700 hover:bg-ink-600 border border-border rounded text-xs text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <PlayIcon className="w-3 h-3" />
        {running ? 'Running…' : 'Run Assessment'}
      </button>
    </button>
  )
}

// ── Question row ──────────────────────────────────────────────────────────────

function QuestionRow({ question: initialQuestion, sourceSlug, activeKB, onRated, onEdited }) {
  const [expanded, setExpanded] = useState(false)
  const [question, setQuestion] = useState(initialQuestion)
  const [editing, setEditing]   = useState(false)
  const [editText, setEditText] = useState(initialQuestion.question)
  const [saving, setSaving]     = useState(false)

  const latestRun = question.runs?.length > 0 ? question.runs[question.runs.length - 1] : null

  const handleRate = async (rating) => {
    try {
      await rateQuestion(sourceSlug, question.id, rating, activeKB)
      onRated()
    } catch (err) {
      console.error(err)
    }
  }

  const startEdit = (e) => {
    e.stopPropagation()
    setEditText(question.question)
    setEditing(true)
  }

  const cancelEdit = () => {
    setEditing(false)
    setEditText(question.question)
  }

  const saveEdit = async () => {
    const trimmed = editText.trim()
    if (!trimmed || trimmed === question.question) { cancelEdit(); return }
    setSaving(true)
    try {
      await updateQuestion(sourceSlug, question.id, trimmed, activeKB)
      setQuestion(q => ({ ...q, question: trimmed }))
      setEditing(false)
      onEdited?.()
    } catch (err) {
      console.error(err)
    } finally {
      setSaving(false)
    }
  }

  const handleEditKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); saveEdit() }
    if (e.key === 'Escape') cancelEdit()
  }

  const hasNewKGs = latestRun && latestRun.new_kgs_registered?.length > 0
  const userRating = latestRun?.user_rating

  return (
    <div className="border border-border rounded-lg overflow-hidden mb-3">
      {/* Header */}
      <div
        className="w-full flex items-start gap-3 px-4 py-3 bg-ink-900 hover:bg-ink-800 transition-colors group"
      >
        <button
          onClick={() => !editing && setExpanded(v => !v)}
          className="flex-shrink-0 w-5 h-5 rounded-full bg-ink-700 border border-border text-xs font-mono text-muted flex items-center justify-center mt-0.5"
        >
          {question.id}
        </button>

        <div className="flex-1 min-w-0" onClick={() => !editing && setExpanded(v => !v)}>
          {editing ? (
            <div onClick={e => e.stopPropagation()} className="space-y-1.5">
              <textarea
                autoFocus
                value={editText}
                onChange={e => setEditText(e.target.value)}
                onKeyDown={handleEditKeyDown}
                rows={3}
                className="w-full bg-ink-800 border border-accent/60 rounded px-2.5 py-1.5 text-sm text-white placeholder:text-muted/50 resize-none focus:outline-none focus:border-accent leading-snug"
              />
              <div className="flex gap-2">
                <button
                  onClick={saveEdit}
                  disabled={saving || !editText.trim()}
                  className="px-2.5 py-1 rounded bg-accent text-white text-xs font-medium hover:bg-accent/80 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
                <button
                  onClick={cancelEdit}
                  className="px-2.5 py-1 rounded bg-ink-700 border border-border text-xs text-muted hover:text-white transition-colors"
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="flex items-start gap-1.5">
                <p className="text-sm text-white leading-snug flex-1">{question.question}</p>
                <button
                  onClick={startEdit}
                  title="Edit question"
                  className="flex-shrink-0 p-0.5 rounded text-muted opacity-0 group-hover:opacity-100 hover:text-white hover:bg-ink-700 transition-all"
                >
                  <PencilIcon className="w-3.5 h-3.5" />
                </button>
              </div>
              {question.linked_kgs?.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mt-1.5">
                  {question.linked_kgs.map(kg => (
                    <span key={kg} className="px-1.5 py-0.5 bg-ink-700 border border-border rounded text-[10px] font-mono text-muted">
                      {kg.split('/').pop()}
                    </span>
                  ))}
                </div>
              )}
            </>
          )}
        </div>

        {!editing && (
          <div
            onClick={() => setExpanded(v => !v)}
            className="flex items-center gap-2 flex-shrink-0 cursor-pointer"
          >
            {hasNewKGs && (
              <span className="flex items-center gap-1 text-xs text-amber-400">
                <ExclamationTriangleIcon className="w-3.5 h-3.5" />
                {latestRun.new_kgs_registered.length} KG
              </span>
            )}
            {latestRun && !hasNewKGs && (
              <CheckCircleIcon className="w-4 h-4 text-green-400" />
            )}
            {expanded
              ? <ChevronDownIcon className="w-4 h-4 text-muted" />
              : <ChevronRightIcon className="w-4 h-4 text-muted" />
            }
          </div>
        )}
      </div>

      {/* Expanded body */}
      {expanded && (
        <div className="px-4 py-4 bg-ink-950 border-t border-border">
          {!latestRun ? (
            <p className="text-sm text-muted italic">Not yet run — click Run Assessment to get an answer.</p>
          ) : (
            <>
              {/* New KG warning */}
              {hasNewKGs && (
                <div className="mb-3 p-3 bg-amber-950/20 border border-amber-800/40 rounded-lg">
                  <p className="text-xs font-semibold text-amber-400 mb-1.5">New KGs registered while answering:</p>
                  <ul className="space-y-1">
                    {latestRun.new_kgs_registered.map(kg => (
                      <li key={kg} className="text-xs font-mono text-amber-300">{kg}</li>
                    ))}
                  </ul>
                </div>
              )}

              {/* Answer */}
              <div className="prose-sm text-white/80 text-sm leading-relaxed mb-3">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {latestRun.answer}
                </ReactMarkdown>
              </div>

              {/* Cost + rating row */}
              <div className="flex items-center justify-between flex-wrap gap-3 pt-3 border-t border-border">
                <CostBadge
                  inputTokens={latestRun.input_tokens}
                  outputTokens={latestRun.output_tokens}
                  costUsd={latestRun.cost_usd}
                />

                <div className="flex items-center gap-2">
                  <span className="text-xs text-muted">Rate this answer:</span>
                  <button
                    onClick={() => handleRate(userRating === true ? null : true)}
                    title="Good answer"
                    className={`p-1.5 rounded transition-colors ${
                      userRating === true
                        ? 'text-green-400 bg-green-950/20'
                        : 'text-muted hover:text-green-400 hover:bg-green-950/20'
                    }`}
                  >
                    {userRating === true
                      ? <HandThumbUpSolid className="w-4 h-4" />
                      : <HandThumbUpIcon className="w-4 h-4" />
                    }
                  </button>
                  <button
                    onClick={() => handleRate(userRating === false ? null : false)}
                    title="Poor answer"
                    className={`p-1.5 rounded transition-colors ${
                      userRating === false
                        ? 'text-red-400 bg-red-950/20'
                        : 'text-muted hover:text-red-400 hover:bg-red-950/20'
                    }`}
                  >
                    {userRating === false
                      ? <HandThumbDownSolid className="w-4 h-4" />
                      : <HandThumbDownIcon className="w-4 h-4" />
                    }
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

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Assess() {
  const { activeKB } = useAppState()
  const [assessments, setAssessments] = useState([])
  const [selectedSlug, setSelectedSlug] = useState(null)
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(false)
  const [runJob, setRunJob] = useState(null) // { job_id }
  const [runStatus, setRunStatus] = useState(null) // 'running' | 'done' | 'error'

  const fetchList = useCallback(async () => {
    try {
      const data = await listAssessments(activeKB)
      setAssessments(data.assessments || [])
    } catch (err) {
      console.error(err)
    }
  }, [activeKB])

  const fetchDetail = useCallback(async (slug) => {
    setLoading(true)
    try {
      const data = await getAssessment(slug, activeKB)
      setDetail(data)
    } catch (err) {
      console.error(err)
    } finally {
      setLoading(false)
    }
  }, [activeKB])

  useEffect(() => { fetchList() }, [fetchList])

  useEffect(() => {
    if (selectedSlug) fetchDetail(selectedSlug)
  }, [selectedSlug, fetchDetail])

  // Poll if there's a running job from the detail panel
  useEffect(() => {
    if (!runJob || runStatus !== 'running') return
    const interval = setInterval(async () => {
      try {
        const job = await assessJobStatus(runJob.job_id)
        if (job.status === 'done' || job.status === 'error') {
          clearInterval(interval)
          setRunStatus(job.status)
          if (job.status === 'done') {
            await fetchList()
            if (selectedSlug) await fetchDetail(selectedSlug)
          }
        }
      } catch {}
    }, 3000)
    return () => clearInterval(interval)
  }, [runJob, runStatus])

  const handleRunFromDetail = async () => {
    if (!selectedSlug) return
    setRunStatus('running')
    try {
      const { job_id } = await runAssessment(selectedSlug, activeKB)
      setRunJob({ job_id })
    } catch (err) {
      console.error(err)
      setRunStatus('error')
    }
  }

  const handleRunDone = async (slug) => {
    await fetchList()
    if (selectedSlug === slug) await fetchDetail(slug)
  }

  const handleRated = async () => {
    if (selectedSlug) await fetchDetail(selectedSlug)
  }

  const passingCount = detail?.questions?.filter(
    q => q.runs?.length > 0 && q.runs[q.runs.length - 1].new_kgs_registered?.length === 0
  ).length ?? 0

  return (
    <div className="flex h-full">
      {/* Left panel — assessment list */}
      <div className="w-80 flex-shrink-0 border-r border-border flex flex-col overflow-hidden">
        <div className="px-4 py-4 border-b border-border">
          <h2 className="text-sm font-semibold text-white">
            Assessments
            {assessments.length > 0 && (
              <span className="ml-2 text-xs font-normal text-muted">{assessments.length}</span>
            )}
          </h2>
          <p className="text-xs text-muted mt-0.5">Auto-generated on ingest</p>
        </div>

        <div className="flex-1 overflow-y-auto">
          {assessments.length === 0 ? (
            <div className="px-4 py-8 text-center">
              <p className="text-sm text-muted">No assessments yet.</p>
              <p className="text-xs text-muted mt-1">Ingest a source to auto-generate one.</p>
            </div>
          ) : (
            assessments.map(a => (
              <AssessmentCard
                key={a.source_slug}
                assessment={a}
                isSelected={selectedSlug === a.source_slug}
                onSelect={setSelectedSlug}
                onRunDone={handleRunDone}
                activeKB={activeKB}
              />
            ))
          )}
        </div>
      </div>

      {/* Right panel — assessment detail */}
      <div className="flex-1 overflow-y-auto">
        {!selectedSlug ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Select an assessment to view questions
          </div>
        ) : loading ? (
          <div className="flex items-center justify-center h-full text-muted text-sm">
            Loading…
          </div>
        ) : detail ? (
          <div className="px-8 py-6 max-w-3xl">
            {/* Header */}
            <div className="flex items-start justify-between gap-4 mb-6">
              <div>
                <h1 className="text-lg font-semibold text-white mb-1">{detail.title}</h1>
                <div className="flex items-center gap-3 text-xs text-muted">
                  <StatusBadge status={detail.status} />
                  <span className="text-border">·</span>
                  <span>
                    {passingCount}/{detail.questions?.length ?? 0} questions passing
                  </span>
                  {detail.last_run && (
                    <>
                      <span className="text-border">·</span>
                      <span>last run {new Date(detail.last_run).toLocaleString()}</span>
                    </>
                  )}
                </div>
              </div>

              <button
                onClick={handleRunFromDetail}
                disabled={runStatus === 'running'}
                className="flex items-center gap-2 px-3 py-2 bg-accent hover:bg-accent/80 rounded text-sm text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex-shrink-0"
              >
                <PlayIcon className="w-4 h-4" />
                {runStatus === 'running' ? 'Running…' : 'Run All'}
              </button>
            </div>

            {runStatus === 'running' && (
              <div className="mb-5 p-3 bg-ink-800 border border-border rounded-lg text-sm text-muted">
                Running 10 questions through the chat pipeline — this may take a minute…
              </div>
            )}

            {runStatus === 'error' && (
              <div className="mb-5 p-3 bg-red-950/20 border border-red-800/40 rounded-lg text-sm text-red-400">
                Assessment run failed. Check the server logs.
              </div>
            )}

            {/* Progress bar */}
            {detail.questions?.length > 0 && (
              <div className="mb-6">
                <div className="flex justify-between text-xs text-muted mb-1.5">
                  <span>Questions with 0 new KGs</span>
                  <span>{passingCount} / {detail.questions.length}</span>
                </div>
                <div className="w-full h-1.5 bg-ink-800 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-green-500 rounded-full transition-all duration-500"
                    style={{ width: `${(passingCount / detail.questions.length) * 100}%` }}
                  />
                </div>
              </div>
            )}

            {/* Questions */}
            <div>
              {detail.questions?.map(q => (
                <QuestionRow
                  key={q.id}
                  question={q}
                  sourceSlug={selectedSlug}
                  activeKB={activeKB}
                  onRated={handleRated}
                  onEdited={handleRated}
                />
              ))}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  )
}
