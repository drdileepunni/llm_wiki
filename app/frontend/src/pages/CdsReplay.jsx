import { useEffect, useRef, useState } from 'react'
import {
  getReplayPatients,
  getReplaySnapshots,
  getReplayResults,
  startReplay,
  getReplayStatus,
  getSnapshotSchedule,
  addToSchedule,
  removeFromSchedule,
  collectNow,
  getSchedulerStatus,
  deleteSnapshot,
  collectWorkspace,
  pauseScheduler,
  resumeScheduler,
} from '../api'
import {
  PlayIcon,
  ClockIcon,
  ArrowUturnRightIcon,
  BoltIcon,
  CalendarIcon,
  TrashIcon,
  CheckCircleIcon,
  FlagIcon,
  XMarkIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ArrowPathIcon,
} from '@heroicons/react/24/outline'

// ── Helpers ────────────────────────────────────────────────────────────────

const IST_TZ = 'Asia/Kolkata'

function fmtTs(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleString('en-IN', { dateStyle: 'short', timeStyle: 'short', timeZone: IST_TZ }) + ' IST'
  } catch { return ts }
}

function fmtShortTime(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', timeZone: IST_TZ })
  } catch { return ts }
}

function Badge({ children, color = 'zinc' }) {
  const cls = {
    zinc:   'bg-zinc-700 text-zinc-200',
    cyan:   'bg-cyan-900/60 text-cyan-300',
    orange: 'bg-orange-900/60 text-orange-300',
    green:  'bg-green-900/60 text-green-300',
    violet: 'bg-violet-900/60 text-violet-300',
    red:    'bg-red-900/60 text-red-300',
  }[color] || 'bg-zinc-700 text-zinc-200'
  return <span className={`inline-block rounded px-1.5 py-0.5 text-[10px] font-mono ${cls}`}>{children}</span>
}

function ProgressBar({ value, total }) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-muted">
        <span>Snapshot {value} / {total}</span>
        <span>{pct}%</span>
      </div>
      <div className="h-1.5 bg-ink-700 rounded-full overflow-hidden">
        <div className="h-full bg-cyan-500 rounded-full transition-all duration-300" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

// ── Status colour mapping ──────────────────────────────────────────────────

const STATUS_META = {
  critical:  { bg: 'bg-red-600',    border: 'border-red-500',    text: 'text-red-300',    dot: 'bg-red-400',    label: 'Critical' },
  worsening: { bg: 'bg-orange-600', border: 'border-orange-500', text: 'text-orange-300', dot: 'bg-orange-400', label: 'Worsening' },
  stable:    { bg: 'bg-yellow-600', border: 'border-yellow-500', text: 'text-yellow-300', dot: 'bg-yellow-400', label: 'Stable' },
  improving: { bg: 'bg-emerald-600',border: 'border-emerald-500',text: 'text-emerald-300',dot: 'bg-emerald-400',label: 'Improving' },
  resolved:  { bg: 'bg-zinc-600',   border: 'border-zinc-500',   text: 'text-zinc-400',   dot: 'bg-zinc-500',   label: 'Resolved' },
}

function statusMeta(status) {
  return STATUS_META[status] || STATUS_META.stable
}

// ── Tooltip ────────────────────────────────────────────────────────────────

function Tooltip({ content, children }) {
  const [show, setShow] = useState(false)
  const [pos, setPos] = useState({ top: 0, left: 0 })
  const ref = useRef(null)

  function handleMouseEnter(e) {
    const rect = ref.current?.getBoundingClientRect()
    if (rect) {
      setPos({ top: rect.bottom + 8, left: rect.left })
    }
    setShow(true)
  }

  return (
    <span
      ref={ref}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={() => setShow(false)}
      className="relative inline-block"
    >
      {children}
      {show && content && (
        <div
          className="fixed z-50 max-w-xs bg-zinc-900 border border-zinc-600 rounded-lg shadow-xl px-3 py-2 text-xs text-zinc-200 leading-relaxed pointer-events-none"
          style={{ top: pos.top, left: pos.left }}
        >
          {content}
        </div>
      )}
    </span>
  )
}

// ── Problem cell (one cell in the swimlane grid) ───────────────────────────

function ProblemCell({ problem, snapshotIdx, isLatest, onClick }) {
  if (!problem) {
    return (
      <div className="h-10 flex items-center justify-center">
        <div className="w-2 h-2 rounded-full bg-zinc-700 opacity-40" />
      </div>
    )
  }

  const meta = statusMeta(problem.status)
  const hasPivot = !!problem.plan_changing_event

  return (
    <Tooltip
      content={
        <div className="space-y-1.5">
          <p className="font-semibold text-white">{problem.name} — <span className={meta.text}>{meta.label}</span></p>
          <p><span className="text-zinc-400">Mx:</span> {problem.management}</p>
          <p><span className="text-zinc-400">Now:</span> {problem.current_state}</p>
          {hasPivot && (
            <p className="text-amber-300 border-t border-zinc-700 pt-1">
              <span className="font-semibold">Key event:</span> {problem.plan_changing_event}
            </p>
          )}
        </div>
      }
    >
      <button
        onClick={() => onClick(snapshotIdx, problem)}
        className={`relative h-10 w-full rounded flex items-center justify-center gap-1 border transition-all hover:opacity-90 hover:scale-105 ${meta.bg} ${meta.border} bg-opacity-80`}
      >
        {hasPivot && (
          <FlagIcon className="absolute top-0.5 right-0.5 w-2.5 h-2.5 text-amber-300" />
        )}
        {isLatest && (
          <span className="absolute -top-1 -right-1 w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
        )}
        <span className={`text-[9px] font-bold uppercase tracking-wide text-white`}>
          {meta.label.slice(0, 3)}
        </span>
      </button>
    </Tooltip>
  )
}

// ── Delta badges with hover tooltip (labs + notes) ────────────────────────

function stripEntities(str) {
  return (str || '').replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
}

function DeltaBadges({ result }) {
  const ds = result.delta_summary || {}
  const dc = result.delta_content || {}
  const labs  = dc.new_labs  || []
  const notes = dc.new_notes || []

  const tooltipContent = (labs.length || notes.length) ? (
    <div className="space-y-2 max-w-sm text-left">
      {labs.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-violet-400 uppercase tracking-wider mb-1">New Labs</p>
          {labs.map((l, i) => (
            <div key={i} className="mb-1">
              <span className="text-zinc-300 font-medium">{l.name}</span>
              {l.values && <span className="text-zinc-400"> — {l.values}</span>}
            </div>
          ))}
        </div>
      )}
      {notes.length > 0 && (
        <div>
          <p className="text-[10px] font-semibold text-orange-400 uppercase tracking-wider mb-1">New Notes</p>
          {notes.map((n, i) => (
            <div key={i} className="mb-1.5">
              <p className="text-zinc-400 text-[10px]">{n.note_type}{n.author ? ` · ${n.author}` : ''}</p>
              <p className="text-zinc-300 leading-relaxed">{stripEntities(n.text)}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  ) : null

  const badges = (
    <div className="flex items-center gap-0.5 cursor-default">
      {ds.new_vitals_count > 0 && <span className="text-[8px] text-cyan-500">{ds.new_vitals_count}V</span>}
      {ds.new_labs_count  > 0 && <span className="text-[8px] text-violet-400">{ds.new_labs_count}L</span>}
      {ds.new_notes_count > 0 && <span className="text-[8px] text-orange-400">{ds.new_notes_count}N</span>}
    </div>
  )

  if (!tooltipContent) return badges

  return <Tooltip content={tooltipContent}>{badges}</Tooltip>
}

// ── Order suggestion cards (right panel) ──────────────────────────────────

const ORDER_TYPE_META = {
  medication:    { label: 'Med',   cls: 'bg-blue-800/60 text-blue-300 border-blue-700' },
  antibiotic:    { label: 'Abx',   cls: 'bg-teal-800/60 text-teal-300 border-teal-700' },
  lab:           { label: 'Lab',   cls: 'bg-violet-800/60 text-violet-300 border-violet-700' },
  fluid:         { label: 'Fluid', cls: 'bg-cyan-800/60 text-cyan-300 border-cyan-700' },
  blood_product: { label: 'Blood', cls: 'bg-red-800/60 text-red-300 border-red-700' },
  ventilator:    { label: 'Vent',  cls: 'bg-orange-800/60 text-orange-300 border-orange-700' },
  monitoring:    { label: 'Mon',   cls: 'bg-zinc-700/60 text-zinc-300 border-zinc-600' },
  procedure:     { label: 'Proc',  cls: 'bg-amber-800/60 text-amber-300 border-amber-700' },
}

function orderTypeMeta(orderType) {
  const t = (orderType || '').toLowerCase()
  for (const [key, meta] of Object.entries(ORDER_TYPE_META)) {
    if (t.includes(key)) return meta
  }
  return { label: 'Order', cls: 'bg-zinc-700/60 text-zinc-300 border-zinc-600' }
}

const CONFIDENCE_CLS = {
  high:   'bg-emerald-800/60 text-emerald-300',
  medium: 'bg-yellow-800/60 text-yellow-300',
  low:    'bg-zinc-700/60 text-zinc-400',
}

function ConfidenceBadge({ confidence }) {
  const level = (confidence || '').toLowerCase()
  const cls = CONFIDENCE_CLS[level] || CONFIDENCE_CLS.low
  return (
    <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded ${cls}`}>
      {level || 'low'}
    </span>
  )
}

function OrderCard({ order }) {
  const typeMeta = orderTypeMeta(order.order_type)
  const isSuppressed = !!order.suppressed
  const action = (order.action || 'new').toUpperCase()

  return (
    <div className={`rounded-lg border bg-ink-900/60 overflow-hidden ${isSuppressed ? 'opacity-50 border-zinc-700' : 'border-zinc-600'}`}>
      <div className="flex items-center gap-1.5 px-3 py-2 border-b border-border/40">
        <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded border ${typeMeta.cls}`}>
          {typeMeta.label}
        </span>
        <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${
          action === 'NEW'  ? 'bg-emerald-900/60 text-emerald-300' :
          action === 'STOP' ? 'bg-red-900/60 text-red-300' :
                              'bg-yellow-900/60 text-yellow-300'
        }`}>
          {action}
        </span>
        {isSuppressed && (
          <span className="text-[9px] bg-orange-900/50 text-orange-400 px-1.5 py-0.5 rounded ml-auto">
            SUPPRESSED
          </span>
        )}
        {!isSuppressed && (
          <span className="ml-auto">
            <ConfidenceBadge confidence={order.confidence} />
          </span>
        )}
      </div>
      <div className="px-3 py-2">
        <p className="text-xs font-semibold text-white leading-snug">{order.orderable_name}</p>
        {(() => {
          const det = order.order_details
          const detail = typeof det === 'string' ? det : (det?.instructions || det?.frequency || null)
          return detail ? (
            <p className="text-[10px] text-zinc-400 mt-0.5 leading-relaxed">{detail}</p>
          ) : null
        })()}
      </div>
    </div>
  )
}

function AgentOrdersPanel({ results, selectedSnapIdx, onSelectSnap }) {
  if (!results.length) return null
  const idx = selectedSnapIdx ?? results.length - 1
  const result = results[idx]
  const orders = result?.agent_suggestions || []
  const active = orders.filter(o => !o.suppressed)
  const suppressed = orders.filter(o => o.suppressed)

  return (
    <div className="flex flex-col h-full bg-ink-800 border-l border-border">
      <div className="shrink-0 px-4 py-3 border-b border-border">
        <p className="text-[10px] font-semibold text-muted uppercase tracking-wider">Agent Suggestions</p>
        <p className="text-[9px] text-zinc-600 mt-0.5">Shadow trial — not executed</p>
      </div>

      {/* Snapshot selector chips */}
      <div className="shrink-0 px-3 py-2 border-b border-border flex gap-1 flex-wrap overflow-x-auto">
        {results.map((r, i) => (
          <button
            key={i}
            onClick={() => onSelectSnap(i)}
            className={`text-[9px] font-mono px-2 py-0.5 rounded transition-colors ${
              i === idx
                ? 'bg-cyan-700 text-white'
                : 'bg-ink-700 text-zinc-400 hover:text-white'
            }`}
          >
            {fmtShortTime(r.snapshot_at)}
            {r.agent_suggestions?.length > 0 && (
              <span className="ml-1 text-cyan-400">{r.agent_suggestions.length}</span>
            )}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-2">
        {orders.length === 0 && (
          <p className="text-xs text-zinc-600 italic text-center py-6">
            No suggestions for this snapshot.
          </p>
        )}
        {active.length > 0 && (
          <>
            {active.map((o, i) => <OrderCard key={i} order={o} />)}
          </>
        )}
        {suppressed.length > 0 && (
          <>
            <p className="text-[9px] font-semibold text-zinc-600 uppercase tracking-wider pt-2">
              Suppressed
            </p>
            {suppressed.map((o, i) => <OrderCard key={i} order={o} />)}
          </>
        )}
      </div>
    </div>
  )
}

// ── Patient Timeline (the core visualisation) ──────────────────────────────

function PatientTimeline({ results, snapshots, onReplayFrom, isRunning, onSnapshotDeleted }) {
  const [selected, setSelected] = useState(null)  // { snapIdx, problem }

  if (!results.length) return null

  // ── Collect all unique problem names across snapshots (insertion order) ──
  const problemNames = []
  const seenNames = new Set()
  for (const r of results) {
    for (const p of (r.structured_summary?.problems || [])) {
      if (!seenNames.has(p.name)) {
        seenNames.add(p.name)
        problemNames.push(p.name)
      }
    }
  }

  // ── Build lookup: snapIdx → problem name → problem object ──
  const grid = results.map(r => {
    const map = {}
    for (const p of (r.structured_summary?.problems || [])) {
      map[p.name] = p
    }
    return map
  })

  // ── Resolved problems from latest snapshot ──
  // Filter out any name already tracked in the active grid (LLM sometimes puts
  // resolved problems in both `problems` and `resolved_problems`).
  const latestResolved = (
    results[results.length - 1]?.structured_summary?.resolved_problems || []
  ).filter(r => {
    const name = (typeof r === 'string' ? r.split(':')[0] : String(r)).trim()
    return !seenNames.has(name)
  })

  // ── Admission narrative from first snapshot ──
  const admissionNarrative = results[0]?.structured_summary?.admission_narrative || ''

  return (
    <div className="space-y-4">
      {/* Patient banner */}
      {admissionNarrative && (
        <div className="bg-ink-800 border border-border rounded-lg px-4 py-3">
          <p className="text-[10px] font-semibold text-cyan-400 uppercase tracking-wider mb-1">Admission</p>
          <p className="text-sm text-zinc-200 leading-relaxed">{admissionNarrative}</p>
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <Badge color="cyan">{problemNames.length} tracked problems</Badge>
            {latestResolved.length > 0 && <Badge color="green">{latestResolved.length} resolved</Badge>}
            <Badge color="zinc">{results.length} snapshots</Badge>
          </div>
        </div>
      )}

      {/* Swimlane grid */}
      <div className="bg-ink-800 border border-border rounded-lg overflow-hidden">
        <div className="px-4 py-2 border-b border-border flex items-center justify-between">
          <p className="text-[10px] font-semibold text-muted uppercase tracking-wider">Problem Timeline</p>
          <p className="text-[10px] text-zinc-600">Hover for detail · click to expand</p>
        </div>

        <div className="overflow-x-auto">
          <table className="text-xs border-collapse" style={{ width: 'max-content' }}>
            <thead>
              <tr className="border-b border-border">
                {/* Problem name column */}
                <th className="text-left px-3 py-2 text-[10px] text-muted font-normal w-36 shrink-0 bg-ink-900/50">
                  Problem
                </th>
                {/* One column per snapshot */}
                {results.map((r, i) => (
                  <th key={i} className="px-1 py-2 text-center min-w-[52px]">
                    <div className="flex flex-col items-center gap-0.5">
                      <span className="text-[9px] font-mono text-muted">{fmtShortTime(r.snapshot_at)}</span>
                      <DeltaBadges result={r} />
                      <button
                        onClick={() => onReplayFrom(i)}
                        disabled={isRunning}
                        title={`Replay from snapshot ${i + 1}`}
                        className="text-[8px] text-cyan-600 hover:text-cyan-400 disabled:opacity-30"
                      >
                        <ArrowUturnRightIcon className="w-2.5 h-2.5" />
                      </button>
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {problemNames.map((name, pi) => (
                <tr key={name} className={`border-b border-border/40 ${pi % 2 === 0 ? '' : 'bg-ink-900/20'}`}>
                  <td className="px-3 py-1.5 text-xs text-zinc-300 font-medium bg-ink-900/30 sticky left-0">
                    <Tooltip
                      content={
                        (() => {
                          const firstAppearance = results.find(r => grid[results.indexOf(r)]?.[name])
                          const p = firstAppearance ? grid[results.indexOf(firstAppearance)]?.[name] : null
                          return p ? (
                            <div className="space-y-1">
                              <p className="font-semibold text-white">{p.name}</p>
                              <p><span className="text-zinc-400">Presentation:</span> {p.presenting_features}</p>
                              <p><span className="text-zinc-400">Workup:</span> {p.workup}</p>
                            </div>
                          ) : null
                        })()
                      }
                    >
                      <span className="truncate block max-w-[128px] cursor-help">{name}</span>
                    </Tooltip>
                  </td>
                  {results.map((r, si) => (
                    <td key={si} className="px-1 py-1">
                      <ProblemCell
                        problem={grid[si]?.[name] || null}
                        snapshotIdx={si}
                        isLatest={si === results.length - 1 && !!grid[si]?.[name]}
                        onClick={(snapIdx, problem) => setSelected({ snapIdx, problem })}
                      />
                    </td>
                  ))}
                </tr>
              ))}

              {/* Resolved row — greyed out, last */}
              {latestResolved.map((r, i) => (
                <tr key={`resolved-${i}`} className="border-b border-border/20 opacity-50">
                  <td className="px-3 py-1.5 text-xs text-zinc-500 bg-ink-900/30 sticky left-0 italic">
                    {typeof r === 'string' ? r.split(':')[0] : r}
                  </td>
                  {results.map((_, si) => (
                    <td key={si} className="px-1 py-1">
                      <div className="h-10 flex items-center justify-center">
                        <div className="w-full h-0.5 bg-zinc-700 rounded" />
                      </div>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Snapshot narrative strip */}
      {selected !== null && (
        <SnapshotDrawer
          result={results[selected.snapIdx]}
          snapIdx={selected.snapIdx}
          focusedProblem={selected.problem}
          onClose={() => setSelected(null)}
          onReplayFrom={onReplayFrom}
          isRunning={isRunning}
        />
      )}

      {/* Snapshot list for delete */}
      <SnapshotDeleteStrip snapshots={snapshots} isRunning={isRunning} onReplayFrom={onReplayFrom} onDeleted={onSnapshotDeleted} />
    </div>
  )
}

// ── Snapshot detail drawer ─────────────────────────────────────────────────

function SnapshotDrawer({ result, snapIdx, focusedProblem, onClose, onReplayFrom, isRunning }) {
  const ss = result?.structured_summary || {}
  const problems = ss.problems || []
  const [expandedProblem, setExpandedProblem] = useState(focusedProblem?.name || null)

  return (
    <div className="bg-ink-800 border border-cyan-700/50 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-ink-900/40">
        <div className="flex items-center gap-3">
          <div className="w-6 h-6 rounded-full bg-cyan-600 flex items-center justify-center text-[10px] font-bold text-white shrink-0">
            {snapIdx + 1}
          </div>
          <div>
            <p className="text-sm font-medium text-white">{fmtTs(result.snapshot_at)}</p>
            <div className="flex items-center gap-1.5 mt-0.5">
              {result.delta_summary?.new_vitals_count > 0 && <Badge color="cyan">{result.delta_summary.new_vitals_count}V</Badge>}
              {result.delta_summary?.new_labs_count > 0 && <Badge color="violet">{result.delta_summary.new_labs_count}L</Badge>}
              {result.delta_summary?.new_notes_count > 0 && <Badge color="orange">{result.delta_summary.new_notes_count}N</Badge>}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => onReplayFrom(snapIdx)}
            disabled={isRunning}
            className="flex items-center gap-1 px-2 py-1 rounded text-[10px] font-mono text-cyan-400 bg-cyan-900/30 hover:bg-cyan-900/60 disabled:opacity-30 transition-colors"
          >
            <ArrowUturnRightIcon className="w-3 h-3" />
            replay from here
          </button>
          <button onClick={onClose} className="p-1 text-muted hover:text-white transition-colors">
            <XMarkIcon className="w-4 h-4" />
          </button>
        </div>
      </div>

      <div className="px-4 py-4 space-y-4">
        {/* Narrative paragraph */}
        {result.running_summary && (
          <div>
            <p className="text-[10px] font-semibold text-zinc-400 uppercase tracking-wider mb-1.5">Summary</p>
            <p className="text-xs text-zinc-300 leading-relaxed">{result.running_summary}</p>
          </div>
        )}

        {/* Per-problem cards */}
        {problems.length > 0 && (
          <div>
            <p className="text-[10px] font-semibold text-zinc-400 uppercase tracking-wider mb-2">Problems at this snapshot</p>
            <div className="space-y-2">
              {problems.map(p => {
                const meta = statusMeta(p.status)
                const isExpanded = expandedProblem === p.name
                return (
                  <div
                    key={p.name}
                    className={`rounded-lg border overflow-hidden ${meta.border} bg-ink-900/40`}
                  >
                    <button
                      onClick={() => setExpandedProblem(isExpanded ? null : p.name)}
                      className="w-full flex items-center gap-3 px-3 py-2.5 text-left hover:bg-white/5 transition-colors"
                    >
                      <span className={`w-2 h-2 rounded-full shrink-0 ${meta.dot}`} />
                      <span className="text-xs font-semibold text-white flex-1">{p.name}</span>
                      <span className={`text-[10px] font-mono ${meta.text}`}>{meta.label}</span>
                      {p.plan_changing_event && (
                        <FlagIcon className="w-3.5 h-3.5 text-amber-400 shrink-0" title="Care plan changed" />
                      )}
                      {isExpanded
                        ? <ChevronDownIcon className="w-3.5 h-3.5 text-muted shrink-0" />
                        : <ChevronRightIcon className="w-3.5 h-3.5 text-muted shrink-0" />
                      }
                    </button>

                    {isExpanded && (
                      <div className="px-4 pb-3 pt-1 space-y-2 border-t border-border/40">
                        <Row label="Now" value={p.current_state} />
                        <Row label="Mx" value={p.management} />
                        <Row label="Workup" value={p.workup} />
                        <Row label="Presentation" value={p.presenting_features} />
                        {p.plan_changing_event && (
                          <div className="mt-2 bg-amber-900/20 border border-amber-700/40 rounded px-3 py-2">
                            <p className="text-[10px] font-semibold text-amber-400 uppercase tracking-wider mb-0.5">Care plan pivot</p>
                            <p className="text-xs text-amber-200">{p.plan_changing_event}</p>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* Resolved */}
        {ss.resolved_problems?.length > 0 && (
          <div>
            <p className="text-[10px] font-semibold text-zinc-500 uppercase tracking-wider mb-1">Resolved / Improving</p>
            <ul className="space-y-0.5">
              {ss.resolved_problems.map((r, i) => (
                <li key={i} className="text-xs text-zinc-500 flex gap-1.5">
                  <span className="text-zinc-600">·</span>{r}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}

function Row({ label, value }) {
  if (!value) return null
  return (
    <div className="flex gap-2">
      <span className="text-[10px] text-zinc-500 uppercase tracking-wide shrink-0 w-20 pt-0.5">{label}</span>
      <span className="text-xs text-zinc-300 leading-relaxed">{value}</span>
    </div>
  )
}

// ── Snapshot delete strip ──────────────────────────────────────────────────

function SnapshotDeleteStrip({ snapshots, isRunning, onReplayFrom, onDeleted }) {
  const [error, setError] = useState(null)

  if (!snapshots.length) return null

  return (
    <div className="bg-ink-800 border border-border rounded-lg p-4">
      <p className="text-xs font-semibold text-muted mb-2">Raw Snapshots ({snapshots.length})</p>
      <div className="flex flex-wrap gap-1.5">
        {snapshots.map((s, i) => (
          <span key={s.id} className="flex items-center gap-1.5 text-[10px] bg-ink-700 rounded px-2 py-1 text-zinc-300">
            <span className="w-4 h-4 rounded-full bg-cyan-700 flex items-center justify-center text-[9px] font-bold text-white shrink-0">{i + 1}</span>
            <ClockIcon className="w-3 h-3 text-muted shrink-0" />
            {fmtTs(s.snapshot_at)}
            <button
              onClick={() => onReplayFrom?.(i)}
              disabled={isRunning}
              title={`Replay from snapshot ${i + 1}`}
              className="text-cyan-500 hover:text-cyan-300 disabled:opacity-30 transition-colors"
            >
              <ArrowUturnRightIcon className="w-2.5 h-2.5" />
            </button>
            <button
              onClick={async () => {
                if (!window.confirm(`Delete snapshot ${i + 1} (${fmtTs(s.snapshot_at)})?`)) return
                try {
                  await deleteSnapshot(s.id)
                  onDeleted?.()
                } catch (e) { setError(e.message) }
              }}
              title="Delete snapshot"
              className="text-zinc-600 hover:text-red-400 transition-colors"
            >
              <TrashIcon className="w-2.5 h-2.5" />
            </button>
          </span>
        ))}
      </div>
      {error && <p className="text-xs text-red-400 mt-2">{error}</p>}
    </div>
  )
}

// ── Snapshot collector panel ───────────────────────────────────────────────

function SnapshotCollector({ onCollected, selectedCpmrn, selectedEncounter, schedulerSt }) {
  const [cpmrn, setCpmrn]           = useState('')
  const [encounter, setEncounter]   = useState(1)
  const [workspace, setWorkspace]   = useState('1A')

  useEffect(() => {
    if (selectedCpmrn) setCpmrn(selectedCpmrn)
    if (selectedEncounter) setEncounter(selectedEncounter)
  }, [selectedCpmrn, selectedEncounter])

  const [collecting, setCollecting]     = useState(false)
  const [wsCollecting, setWsCollecting] = useState(false)
  const [lastResult, setLastResult]     = useState(null)
  const [wsResult, setWsResult]         = useState(null)
  const [error, setError]               = useState(null)

  async function handleCollect() {
    if (!cpmrn.trim()) return
    setCollecting(true); setError(null); setLastResult(null)
    try {
      const r = await collectNow(cpmrn.trim(), encounter)
      setLastResult(r); onCollected?.()
    } catch (e) { setError(e.message) }
    finally { setCollecting(false) }
  }

  async function handleSchedule() {
    if (!cpmrn.trim()) return
    setError(null)
    try { await addToSchedule(cpmrn.trim(), encounter, workspace.trim() || null); onCollected?.() }
    catch (e) { setError(e.message) }
  }

  async function handleWorkspaceCollect(scheduleAll = false) {
    if (!workspace.trim()) return
    setWsCollecting(true); setError(null); setWsResult(null)
    try {
      const r = await collectWorkspace(workspace.trim(), scheduleAll)
      setWsResult(r); onCollected?.()
    } catch (e) { setError(e.message) }
    finally { setWsCollecting(false) }
  }

  return (
    <div className="bg-ink-800 border border-border rounded-lg p-4 space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold text-white uppercase tracking-wider">Snapshot Collector</p>
        {/* Scheduler status + pause/resume toggle */}
        {schedulerSt?.running && (
          <button
            onClick={schedulerSt.paused ? schedulerSt.onResume : schedulerSt.onPause}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] font-mono transition-colors ${
              schedulerSt.paused
                ? 'text-yellow-400 bg-yellow-400/10 hover:bg-yellow-400/20'
                : 'text-green-400 bg-green-400/10 hover:bg-green-400/20'
            }`}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${schedulerSt.paused ? 'bg-yellow-400' : 'bg-green-400 animate-pulse'}`} />
            {schedulerSt.paused
              ? 'paused · resume'
              : `next ${schedulerSt.next_run ? fmtShortTime(schedulerSt.next_run) : '…'} · pause`}
          </button>
        )}
        {!schedulerSt?.running && (
          <span className="text-[10px] text-zinc-600 font-mono">scheduler off</span>
        )}
      </div>

      <div className="flex items-center gap-2">
        <input
          value={cpmrn}
          onChange={e => setCpmrn(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleCollect()}
          placeholder="CPMRN"
          className="flex-1 bg-ink-700 border border-border rounded px-3 py-1.5 text-sm text-white placeholder-muted focus:outline-none focus:border-cyan-500 font-mono"
        />
        <input
          value={encounter}
          onChange={e => setEncounter(Number(e.target.value) || 1)}
          type="number" min={1}
          className="w-16 bg-ink-700 border border-border rounded px-2 py-1.5 text-sm text-white focus:outline-none focus:border-cyan-500 font-mono"
        />
        <button
          onClick={handleCollect}
          disabled={collecting || !cpmrn.trim()}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 text-sm font-medium transition-colors"
        >
          <BoltIcon className="w-3.5 h-3.5" />
          {collecting ? 'Collecting…' : 'Now'}
        </button>
        <button
          onClick={handleSchedule}
          disabled={!cpmrn.trim()}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-violet-700 hover:bg-violet-600 disabled:opacity-40 text-sm font-medium transition-colors"
        >
          <CalendarIcon className="w-3.5 h-3.5" />
          Schedule
        </button>
      </div>

      {/* Workspace row */}
      <div className="flex items-center gap-2 border-t border-border pt-3">
        <span className="text-[10px] text-muted uppercase tracking-wider shrink-0">Workspace</span>
        <input
          value={workspace}
          onChange={e => setWorkspace(e.target.value.toUpperCase())}
          placeholder="1A"
          className="w-16 bg-ink-700 border border-border rounded px-2 py-1.5 text-sm text-white placeholder-muted focus:outline-none focus:border-violet-500 font-mono uppercase"
        />
        <button
          onClick={() => handleWorkspaceCollect(false)}
          disabled={wsCollecting || !workspace.trim()}
          title="Collect one snapshot for every admitted patient in this workspace"
          className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-cyan-700 hover:bg-cyan-600 disabled:opacity-40 text-sm font-medium transition-colors"
        >
          <BoltIcon className="w-3.5 h-3.5" />
          {wsCollecting ? 'Collecting…' : 'Collect All'}
        </button>
        <button
          onClick={() => handleWorkspaceCollect(true)}
          disabled={wsCollecting || !workspace.trim()}
          title="Collect + add every patient to hourly schedule"
          className="flex items-center gap-1.5 px-3 py-1.5 rounded bg-violet-700 hover:bg-violet-600 disabled:opacity-40 text-sm font-medium transition-colors"
        >
          <CalendarIcon className="w-3.5 h-3.5" />
          Schedule All
        </button>
      </div>

      {/* Feedback */}
      {lastResult && (
        <div className="flex items-center gap-2 text-xs text-green-300">
          <CheckCircleIcon className="w-4 h-4 shrink-0" />
          Collected {lastResult.cpmrn} — {lastResult.vitals_count}V / {lastResult.labs_count}L / {lastResult.notes_count}N
        </div>
      )}
      {wsResult && (
        <div className="text-xs text-green-300 space-y-0.5">
          <div className="flex items-center gap-2">
            <CheckCircleIcon className="w-4 h-4 shrink-0" />
            <span>Workspace {wsResult.workspace} — {wsResult.patients_found} patient{wsResult.patients_found !== 1 ? 's' : ''}</span>
          </div>
          {wsResult.results?.map((r, i) => (
            <p key={i} className="ml-6 text-[11px] font-mono text-zinc-400">
              {r.cpmrn} enc{r.encounter}:&nbsp;
              <span className={r.status === 'error' ? 'text-red-400' : 'text-green-400'}>{r.status}</span>
              {r.vitals_count != null && ` — ${r.vitals_count}V/${r.labs_count}L/${r.notes_count}N`}
            </p>
          ))}
        </div>
      )}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  )
}

// ── Main page ──────────────────────────────────────────────────────────────

export default function CdsReplay() {
  const [patients, setPatients]           = useState([])
  const [selected, setSelected]           = useState(null)
  const [snapshots, setSnapshots]         = useState([])
  const [results, setResults]             = useState([])
  const [runId, setRunId]                 = useState(null)
  const [runState, setRunState]           = useState(null)
  const [error, setError]                 = useState(null)
  const [selectedOrderSnap, setSelectedOrderSnap] = useState(null)
  const [patientSearch, setPatientSearch] = useState('')
  const pollRef                           = useRef(null)

  // ── Schedule + scheduler state (lifted) ───────────────────────────────
  const [schedule, setSchedule]           = useState([])   // [{ CPMRN, encounter, active }]
  const [schedulerSt, setSchedulerSt]     = useState(null) // { running, paused, next_run, … }
  const schedPollRef                      = useRef(null)

  async function refreshScheduler() {
    try {
      const [sch, st] = await Promise.all([getSnapshotSchedule(), getSchedulerStatus()])
      setSchedule(sch.scheduled || [])
      setSchedulerSt(st)
    } catch {}
  }

  useEffect(() => {
    refreshScheduler()
    schedPollRef.current = setInterval(refreshScheduler, 15000)
    return () => clearInterval(schedPollRef.current)
  }, [])

  async function handlePause() {
    try { const st = await pauseScheduler(); setSchedulerSt(st) } catch {}
  }
  async function handleResume() {
    try { const st = await resumeScheduler(); setSchedulerSt(st) } catch {}
  }
  async function handleRemoveFromSchedule(cpmrn, encounter) {
    try { await removeFromSchedule(cpmrn, encounter); await refreshScheduler() } catch {}
  }

  const schedulerStWithCbs = schedulerSt
    ? { ...schedulerSt, onPause: handlePause, onResume: handleResume }
    : null

  // ── Patients ──────────────────────────────────────────────────────────
  useEffect(() => {
    getReplayPatients()
      .then(d => {
        setPatients(d.patients || [])
        if (d.patients?.length === 1) {
          const p = d.patients[0]
          setSelected({ cpmrn: p.cpmrn, encounter: p.encounter })
        }
      })
      .catch(e => setError(e.message))
  }, [])

  useEffect(() => {
    if (!selected) return
    setSnapshots([]); setResults([]); setRunId(null); setRunState(null); setSelectedOrderSnap(null)
    getReplaySnapshots(selected.cpmrn, selected.encounter)
      .then(d => setSnapshots(d.snapshots || [])).catch(() => {})
    getReplayResults(selected.cpmrn, selected.encounter)
      .then(d => setResults(d.results || [])).catch(() => {})
  }, [selected])

  useEffect(() => {
    if (!runId) return
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const st = await getReplayStatus(runId)
        setRunState(st)
        if (st.status === 'done') {
          clearInterval(pollRef.current)
          const d = await getReplayResults(selected.cpmrn, selected.encounter)
          setResults(d.results || [])
        } else if (st.status === 'error') {
          clearInterval(pollRef.current)
          setError(st.error || 'Replay failed')
        }
      } catch {}
    }, 2000)
    return () => clearInterval(pollRef.current)
  }, [runId, selected])

  async function handleStartReplay(fromIndex = 0, force = false) {
    if (!selected) return
    setError(null)
    if (fromIndex === 0 || force) setResults([])
    setRunState(null)
    try {
      const { run_id } = await startReplay(selected.cpmrn, selected.encounter, fromIndex, force)
      setRunId(run_id)
      setRunState({ status: 'running', progress: fromIndex, total: snapshots.length, current_ts: null, from_index: fromIndex, force })
    } catch (e) { setError(e.message) }
  }

  async function handleHardReplay() {
    if (!selected) return
    if (!window.confirm(
      `Hard Replay will delete all existing results for ${selected.cpmrn} and re-run every snapshot from scratch.\n\nContinue?`
    )) return
    handleStartReplay(0, true)
  }

  // Incremental replay — only runs snapshots that don't have results yet
  async function handleIncrementalReplay() {
    if (!selected) return
    const nextIdx = results.length  // results are 0-indexed and contiguous
    if (nextIdx >= snapshots.length) {
      // All snapshots already have results — nothing to do unless forced
      return
    }
    handleStartReplay(nextIdx)
  }

  const isRunning = runState?.status === 'running'
  const isDone    = runState?.status === 'done'

  return (
    <div className="h-full flex flex-col bg-ink-900 text-white overflow-hidden">
      {/* Header */}
      <div className="shrink-0 px-6 py-4 border-b border-border">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold">CDS Replay</h1>
            <p className="text-xs text-muted mt-0.5">Sequential patient timeline across hourly snapshots</p>
          </div>
          {selected && (
            <div className="flex items-center gap-2">
              {/* Incremental — only runs snapshots not yet processed */}
              <button
                onClick={handleIncrementalReplay}
                disabled={isRunning || snapshots.length === 0 || results.length >= snapshots.length}
                title={results.length >= snapshots.length ? 'All snapshots already replayed' : `Run ${snapshots.length - results.length} new snapshot(s)`}
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 disabled:cursor-not-allowed text-sm font-medium transition-colors"
              >
                <PlayIcon className="w-4 h-4" />
                {isRunning && !runState?.force
                  ? 'Replaying…'
                  : results.length > 0 && results.length < snapshots.length
                    ? `Resume (${snapshots.length - results.length} new)`
                    : 'Run Replay'}
              </button>
              {/* Hard replay — deletes all results and re-runs everything */}
              <button
                onClick={handleHardReplay}
                disabled={isRunning || snapshots.length === 0}
                title="Delete all results and re-run every snapshot from scratch"
                className="flex items-center gap-2 px-4 py-2 rounded-lg bg-orange-700 hover:bg-orange-600 disabled:opacity-40 disabled:cursor-not-allowed text-sm font-medium transition-colors"
              >
                <ArrowPathIcon className="w-4 h-4" />
                {isRunning && runState?.force ? 'Replaying…' : 'Hard Replay'}
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-hidden flex">
        {/* Left sidebar — patient selector */}
        <div className="w-56 shrink-0 border-r border-border overflow-y-auto p-4 space-y-2">
          <p className="text-[10px] font-semibold text-muted uppercase tracking-wider mb-2">Patients</p>
          <input
            placeholder="Search CPMRN…"
            value={patientSearch}
            onChange={e => setPatientSearch(e.target.value)}
            className="w-full bg-ink-700 border border-border rounded px-2.5 py-1.5 text-xs text-white placeholder-muted focus:outline-none focus:border-cyan-500 font-mono mb-1"
          />
          {patients.length === 0 && <p className="text-xs text-muted">No snapshots collected yet.</p>}
          {patients.filter(p => !patientSearch.trim() || p.cpmrn.toLowerCase().includes(patientSearch.trim().toLowerCase())).map(p => {
            const active = selected?.cpmrn === p.cpmrn && selected?.encounter === p.encounter
            const isScheduled = schedule.some(s => s.CPMRN === p.cpmrn && s.encounter === p.encounter && s.active)
            return (
              <div
                key={`${p.cpmrn}-${p.encounter}`}
                className={`relative w-full text-left rounded-lg px-3 py-2.5 space-y-1 border transition-colors ${
                  active ? 'border-cyan-600 bg-cyan-900/30' : 'border-border bg-ink-800'
                }`}
              >
                {/* Green live dot + remove-from-schedule button */}
                {isScheduled && (
                  <div className="absolute top-2 right-2 flex items-center gap-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" title="Hourly collection active" />
                    <button
                      onClick={e => { e.stopPropagation(); handleRemoveFromSchedule(p.cpmrn, p.encounter) }}
                      title="Remove from schedule"
                      className="text-zinc-600 hover:text-red-400 transition-colors"
                    >
                      <XMarkIcon className="w-3 h-3" />
                    </button>
                  </div>
                )}
                <button
                  onClick={() => setSelected({ cpmrn: p.cpmrn, encounter: p.encounter })}
                  className="w-full text-left space-y-1"
                >
                  <p className="text-xs font-semibold text-white truncate pr-8">{p.cpmrn}</p>
                  <p className="text-[10px] text-muted">Enc {p.encounter}</p>
                  <Badge color="cyan">{p.snapshot_count} snapshots</Badge>
                  <p className="text-[10px] text-muted">{fmtTs(p.first_snapshot)} → {fmtTs(p.last_snapshot)}</p>
                </button>
              </div>
            )
          })}
        </div>

        {/* Main content + right panel */}
        <div className="flex-1 overflow-hidden flex min-w-0">
        <div className="flex-1 overflow-y-auto p-6 space-y-4 min-w-0">
          <SnapshotCollector
            selectedCpmrn={selected?.cpmrn}
            selectedEncounter={selected?.encounter}
            schedulerSt={schedulerStWithCbs}
            onCollected={() => {
              getReplayPatients()
                .then(d => setPatients(d.patients || [])).catch(() => {})
              refreshScheduler()
              if (selected) {
                getReplaySnapshots(selected.cpmrn, selected.encounter)
                  .then(d => setSnapshots(d.snapshots || [])).catch(() => {})
              }
            }}
          />

          {selected && (
            <>
              {/* Progress while running */}
              {isRunning && runState && (
                <div className={`border rounded-lg p-4 space-y-3 ${runState.force ? 'bg-orange-950/30 border-orange-700/50' : 'bg-ink-800 border-cyan-700/50'}`}>
                  <div className="flex items-center gap-2">
                    <p className={`text-xs font-semibold ${runState.force ? 'text-orange-400' : 'text-cyan-400'}`}>
                      {runState.force ? 'Hard Replay in progress…' : 'Replay in progress…'}
                    </p>
                    {!runState.force && runState.from_index > 0 && (
                      <span className="text-[10px] text-cyan-600 font-mono">continuing from snapshot {runState.from_index + 1}</span>
                    )}
                  </div>
                  <ProgressBar value={runState.progress || 0} total={runState.total || snapshots.length} />
                  {runState.current_ts && (
                    <p className="text-[10px] text-muted">Processing: {fmtTs(runState.current_ts)}</p>
                  )}
                </div>
              )}

              {isDone && results.length > 0 && (
                <div className="bg-green-900/20 border border-green-700/40 rounded-lg px-4 py-2">
                  <p className="text-xs text-green-300">
                    Replay complete — {results.length} snapshots evaluated.
                    Total cost: ${results.reduce((s, r) => s + (r.suggestions?.cost_usd || 0), 0).toFixed(4)}
                  </p>
                </div>
              )}

              {error && (
                <div className="bg-red-900/20 border border-red-700/40 rounded-lg px-4 py-2">
                  <p className="text-xs text-red-300">{error}</p>
                </div>
              )}

              {/* Patient timeline — only when we have results with structured summaries */}
              {results.length > 0 && (
                <PatientTimeline
                  results={results}
                  snapshots={snapshots}
                  onReplayFrom={handleStartReplay}
                  isRunning={isRunning}
                  onSnapshotDeleted={() => {
                    getReplaySnapshots(selected.cpmrn, selected.encounter)
                      .then(d => setSnapshots(d.snapshots || [])).catch(() => {})
                  }}
                />
              )}

              {/* Prompt to run replay if we have snapshots but no results */}
              {results.length === 0 && snapshots.length > 0 && !isRunning && (
                <>
                  <div className="bg-ink-800 border border-border rounded-lg p-6 text-center space-y-2">
                    <p className="text-sm text-zinc-300">{snapshots.length} snapshots available</p>
                    <p className="text-xs text-muted">Click <span className="text-cyan-400 font-medium">Run Replay</span> to build the patient timeline.</p>
                  </div>
                  <SnapshotDeleteStrip
                    snapshots={snapshots}
                    isRunning={isRunning}
                    onReplayFrom={handleStartReplay}
                    onDeleted={() => {
                      getReplaySnapshots(selected.cpmrn, selected.encounter)
                        .then(d => setSnapshots(d.snapshots || [])).catch(() => {})
                    }}
                  />
                </>
              )}
            </>
          )}
        </div>

        {/* Right panel — agent order suggestions */}
        {results.length > 0 && (
          <div className="w-72 shrink-0">
            <AgentOrdersPanel
              results={results}
              selectedSnapIdx={selectedOrderSnap}
              onSelectSnap={setSelectedOrderSnap}
            />
          </div>
        )}
        </div>
      </div>
    </div>
  )
}
