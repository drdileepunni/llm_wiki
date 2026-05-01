import { useState, useEffect, useRef } from 'react'
import {
  startViva,
  runVivaTurn,
  listVivaSessions,
  getVivaSession,
  deleteVivaSession,
  forkVivaSession,
} from '../api'
import { useAppState } from '../AppStateContext'
import {
  AcademicCapIcon,
  PlayIcon,
  TrashIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CheckCircleIcon,
  ClockIcon,
  SparklesIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline'

const MODEL_OPTIONS = [
  { value: '', label: 'Default (env)' },
  { value: 'claude-opus-4-7', label: 'Claude Opus 4.7' },
  { value: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
  { value: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash' },
  { value: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
]

const PHASE_COLOR = {
  EVOLVING:      'text-blue-400 bg-blue-400/10',
  ESCALATION:    'text-yellow-400 bg-yellow-400/10',
  DETERIORATION: 'text-red-400 bg-red-400/10',
  MANAGEMENT:    'text-green-400 bg-green-400/10',
  LATE:          'text-purple-400 bg-purple-400/10',
}
const DIFF_COLOR = {
  EASY:   'text-emerald-400 bg-emerald-400/10',
  MEDIUM: 'text-amber-400 bg-amber-400/10',
  HARD:   'text-red-400 bg-red-400/10',
}

function badge(label, colorClass) {
  return (
    <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded uppercase tracking-wider ${colorClass}`}>
      {label}
    </span>
  )
}

function TurnCard({ turn, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  const scenario = turn.scenario || {}
  const snap = turn.student_snap || {}

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-4 py-3 bg-ink-800 hover:bg-ink-700 transition-colors text-left"
      >
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-muted">Turn {turn.turn_num}</span>
          {scenario.phase && badge(scenario.phase, PHASE_COLOR[scenario.phase] || 'text-muted bg-ink-700')}
          {scenario.difficulty && badge(scenario.difficulty, DIFF_COLOR[scenario.difficulty] || 'text-muted bg-ink-700')}
          {turn.gaps_resolved > 0 && (
            <span className="text-[10px] text-teal-400 font-mono">
              +{turn.gaps_resolved} gap{turn.gaps_resolved !== 1 ? 's' : ''} resolved
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {turn.cost_usd != null && (
            <span className="text-[10px] text-muted font-mono">${turn.cost_usd.toFixed(4)}</span>
          )}
          {open
            ? <ChevronDownIcon className="w-4 h-4 text-muted" />
            : <ChevronRightIcon className="w-4 h-4 text-muted" />}
        </div>
      </button>

      {open && (
        <div className="divide-y divide-border">
          {/* Scenario */}
          <div className="px-4 py-3 bg-ink-900/50">
            <p className="text-[10px] uppercase tracking-widest text-muted mb-2">Clinical Context</p>
            <p className="text-sm text-white leading-relaxed">{scenario.clinical_context}</p>
            {scenario.question && (
              <p className="mt-2 text-sm text-accent font-medium italic">{scenario.question}</p>
            )}
          </div>

          {/* Student answer */}
          <div className="px-4 py-3">
            <p className="text-[10px] uppercase tracking-widest text-muted mb-2">Student Answer</p>
            {snap.immediate_next_steps?.length > 0 && (
              <div className="mb-3">
                <p className="text-[10px] text-muted uppercase tracking-wider mb-1">Immediate Steps</p>
                <ol className="space-y-1">
                  {snap.immediate_next_steps.map((s, i) => (
                    <li key={i} className="flex gap-2 text-sm text-white">
                      <span className="flex-shrink-0 w-5 h-5 rounded-full bg-accent/20 text-accent text-[10px] flex items-center justify-center font-mono">{i + 1}</span>
                      <span>{s}</span>
                    </li>
                  ))}
                </ol>
              </div>
            )}
            {snap.monitoring_followup?.length > 0 && (
              <div>
                <p className="text-[10px] text-muted uppercase tracking-wider mb-1">Monitoring</p>
                <ul className="space-y-0.5">
                  {snap.monitoring_followup.map((s, i) => (
                    <li key={i} className="text-sm text-white/80 flex gap-2">
                      <span className="text-muted mt-0.5">›</span>{s}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {snap.pages_consulted?.length > 0 && (
              <p className="mt-2 text-[10px] text-muted">
                Wiki: {snap.pages_consulted.join(', ')}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function NewVivaForm({ onStart }) {
  const [topic, setTopic] = useState('')
  const [model, setModel] = useState('')
  const [maxTurns, setMaxTurns] = useState(8)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  async function handleStart() {
    if (!topic.trim()) return
    setLoading(true)
    setError(null)
    try {
      await onStart(topic.trim(), maxTurns, model || null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center h-full">
      <div className="w-full max-w-lg space-y-5 p-8">
        <div className="flex items-center gap-3 mb-2">
          <AcademicCapIcon className="w-6 h-6 text-accent" />
          <h2 className="text-lg font-semibold text-white">New Clinical Viva</h2>
        </div>

        <div>
          <label className="block text-xs text-muted mb-1.5 uppercase tracking-wider">
            Viva Topic
          </label>
          <textarea
            value={topic}
            onChange={e => setTopic(e.target.value)}
            rows={2}
            placeholder="e.g. hyperkalemic emergency in CKD, septic shock management, DKA in ICU…"
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder-muted focus:outline-none focus:border-accent resize-none"
          />
        </div>

        <div className="flex gap-3">
          <div className="flex-1">
            <label className="block text-xs text-muted mb-1.5 uppercase tracking-wider">Model</label>
            <select
              value={model}
              onChange={e => setModel(e.target.value)}
              className="w-full bg-ink-800 border border-border rounded px-2 py-2 text-sm text-white focus:outline-none focus:border-accent"
            >
              {MODEL_OPTIONS.map(o => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </div>
          <div className="w-28">
            <label className="block text-xs text-muted mb-1.5 uppercase tracking-wider">Max Turns</label>
            <input
              type="number"
              min={3}
              max={15}
              value={maxTurns}
              onChange={e => setMaxTurns(Number(e.target.value))}
              className="w-full bg-ink-800 border border-border rounded px-2 py-2 text-sm text-white focus:outline-none focus:border-accent"
            />
          </div>
        </div>

        {error && (
          <p className="text-red-400 text-sm">{error}</p>
        )}

        <button
          onClick={handleStart}
          disabled={!topic.trim() || loading}
          className="w-full flex items-center justify-center gap-2 bg-accent text-black font-medium text-sm px-4 py-2.5 rounded hover:bg-accent/90 disabled:opacity-40 transition-colors"
        >
          {loading
            ? <><ClockIcon className="w-4 h-4 animate-spin" /> Generating scenario…</>
            : <><PlayIcon className="w-4 h-4" /> Start Viva</>}
        </button>
      </div>
    </div>
  )
}

export default function VivaSession() {
  const { activeKB } = useAppState()
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [session, setSession] = useState(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)
  const bottomRef = useRef(null)

  useEffect(() => {
    listVivaSessions(activeKB)
      .then(d => setSessions(d.sessions || []))
      .catch(() => {})
  }, [activeKB])

  useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [session?.turns?.length])

  async function handleStart(topic, maxTurns, model) {
    const data = await startViva(topic, maxTurns, model, activeKB)
    const s = data.session
    setSession(s)
    setActiveId(s.session_id)
    setSessions(prev => [
      {
        session_id: s.session_id,
        topic: s.topic,
        status: s.status,
        current_turn: s.current_turn,
        max_turns: s.max_turns,
        created_at: s.created_at,
        total_cost_usd: s.total_cost_usd,
        outcome: s.outcome,
      },
      ...prev,
    ])
  }

  async function handleSelectSession(id) {
    setActiveId(id)
    setError(null)
    try {
      const data = await getVivaSession(id, activeKB)
      setSession(data.session)
    } catch (e) {
      setError(e.message)
    }
  }

  async function handleRunTurn() {
    if (!session || running) return
    setRunning(true)
    setError(null)
    try {
      const data = await runVivaTurn(session.session_id, null, activeKB)
      setSession(data.session)
      setSessions(prev =>
        prev.map(s =>
          s.session_id === data.session.session_id
            ? {
                ...s,
                status: data.session.status,
                current_turn: data.session.current_turn,
                total_cost_usd: data.session.total_cost_usd,
                outcome: data.session.outcome,
              }
            : s
        )
      )
    } catch (e) {
      setError(e.message)
    } finally {
      setRunning(false)
    }
  }

  async function handleFork(id) {
    try {
      const data = await forkVivaSession(id, activeKB)
      const s = data.session
      setSession(s)
      setActiveId(s.session_id)
      setSessions(prev => [
        {
          session_id: s.session_id,
          topic: s.topic,
          status: s.status,
          current_turn: s.current_turn,
          max_turns: s.max_turns,
          created_at: s.created_at,
          total_cost_usd: s.total_cost_usd,
          outcome: s.outcome,
        },
        ...prev,
      ])
    } catch (e) {
      setError(e.message)
    }
  }

  async function handleDelete(id) {
    if (!window.confirm('Delete this viva session?')) return
    try {
      await deleteVivaSession(id, activeKB)
      setSessions(prev => prev.filter(s => s.session_id !== id))
      if (activeId === id) {
        setActiveId(null)
        setSession(null)
      }
    } catch (e) {
      setError(e.message)
    }
  }

  const isComplete = session?.status === 'complete'
  const pendingScenario = session?.next_scenario
  const turns = session?.turns || []

  return (
    <div className="flex h-full">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 flex flex-col border-r border-border bg-ink-900">
        <div className="px-4 py-4 border-b border-border">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <AcademicCapIcon className="w-4 h-4 text-accent" />
            Clinical Viva
          </h2>
        </div>

        <div className="flex-1 overflow-y-auto py-2">
          {/* New session entry */}
          <button
            onClick={() => { setActiveId(null); setSession(null); setError(null) }}
            className={`w-full text-left px-4 py-3 text-sm transition-colors ${
              activeId === null
                ? 'bg-accent/10 text-accent border-l-2 border-accent pl-[14px]'
                : 'text-muted hover:text-white hover:bg-ink-700'
            }`}
          >
            + New Viva
          </button>

          {sessions.map(s => (
            <div key={s.session_id} className="relative group">
              <button
                onClick={() => handleSelectSession(s.session_id)}
                className={`w-full text-left px-4 py-3 transition-colors ${
                  activeId === s.session_id
                    ? 'bg-accent/10 text-accent border-l-2 border-accent pl-[14px]'
                    : 'text-muted hover:text-white hover:bg-ink-700'
                }`}
              >
                <p className="text-sm font-medium truncate">{s.topic}</p>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className={`text-[10px] font-mono ${s.status === 'complete' ? 'text-green-400' : 'text-yellow-400'}`}>
                    {s.status === 'complete' ? 'complete' : `turn ${s.current_turn}/${s.max_turns}`}
                  </span>
                  <span className="text-[10px] text-muted font-mono">${(s.total_cost_usd || 0).toFixed(3)}</span>
                </div>
              </button>
              <button
                onClick={() => handleDelete(s.session_id)}
                className="absolute right-2 top-1/2 -translate-y-1/2 opacity-0 group-hover:opacity-100 p-1 rounded text-muted hover:text-red-400 transition-all"
              >
                <TrashIcon className="w-3.5 h-3.5" />
              </button>
            </div>
          ))}
        </div>
      </aside>

      {/* Main */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {activeId === null && !session ? (
          <NewVivaForm onStart={handleStart} />
        ) : session ? (
          <>
            {/* Session header */}
            <div className="px-6 py-4 border-b border-border flex items-center justify-between flex-shrink-0">
              <div>
                <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                  {session.topic}
                  {session.forked_from && (
                    <span className="text-[10px] font-mono text-purple-400 bg-purple-400/10 px-1.5 py-0.5 rounded">
                      replay
                    </span>
                  )}
                </h2>
                <p className="text-xs text-muted mt-0.5 font-mono">
                  {session.session_id} · turn {session.current_turn}/{session.max_turns} · ${(session.total_cost_usd || 0).toFixed(4)}
                  {session.forked_from && (
                    <span className="ml-2 text-purple-400">↩ {session.forked_from}</span>
                  )}
                </p>
              </div>
              <div className="flex items-center gap-3">
                {isComplete && !session.forked_from && (
                  <button
                    onClick={() => handleFork(session.session_id)}
                    title="Replay the same scenarios after gap resolution"
                    className="flex items-center gap-1.5 text-xs text-purple-400 hover:text-purple-300 border border-purple-400/30 hover:border-purple-400/60 px-2.5 py-1.5 rounded transition-colors"
                  >
                    <ArrowPathIcon className="w-3.5 h-3.5" />
                    Fork &amp; Replay
                  </button>
                )}
                {isComplete && (
                  <span className="flex items-center gap-1.5 text-green-400 text-xs font-medium">
                    <CheckCircleIcon className="w-4 h-4" />
                    Complete
                  </span>
                )}
              </div>
            </div>

            {/* Turn list */}
            <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
              {turns.map((turn, i) => (
                <TurnCard
                  key={turn.turn_num}
                  turn={turn}
                  defaultOpen={i === turns.length - 1}
                />
              ))}

              {/* Outcome banner */}
              {isComplete && session.outcome && (
                <div className="border border-green-500/30 rounded-lg px-4 py-3 bg-green-500/5">
                  <p className="text-[10px] uppercase tracking-widest text-green-400 mb-1">Case Outcome</p>
                  <p className="text-sm text-white">{session.outcome}</p>
                </div>
              )}

              {/* Next scenario preview */}
              {!isComplete && pendingScenario && turns.length > 0 && (
                <div className="border border-accent/20 rounded-lg px-4 py-3 bg-accent/5">
                  <div className="flex items-center gap-2 mb-2">
                    <SparklesIcon className="w-3.5 h-3.5 text-accent" />
                    <p className="text-[10px] uppercase tracking-widest text-accent">Next Question Ready</p>
                  </div>
                  <p className="text-sm text-white/80 italic">{pendingScenario.clinical_context}</p>
                  <p className="text-sm text-accent mt-1 font-medium">{pendingScenario.question}</p>
                </div>
              )}

              {error && (
                <p className="text-red-400 text-sm px-1">{error}</p>
              )}

              <div ref={bottomRef} />
            </div>

            {/* Run turn button */}
            {!isComplete && (
              <div className="px-6 py-4 border-t border-border flex-shrink-0">
                <button
                  onClick={handleRunTurn}
                  disabled={running}
                  className="flex items-center gap-2 bg-accent text-black font-medium text-sm px-5 py-2.5 rounded hover:bg-accent/90 disabled:opacity-40 transition-colors"
                >
                  {running ? (
                    <>
                      <ClockIcon className="w-4 h-4 animate-spin" />
                      Running turn {session.current_turn + 1}… (student → gaps → teacher)
                    </>
                  ) : (
                    <>
                      <PlayIcon className="w-4 h-4" />
                      Run Turn {session.current_turn + 1}
                    </>
                  )}
                </button>
                <p className="text-[10px] text-muted mt-1.5">
                  Each turn: student answers → knowledge gaps resolved → teacher advances the case
                </p>
              </div>
            )}
          </>
        ) : null}
      </div>
    </div>
  )
}
