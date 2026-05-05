import { useState, useEffect, useRef } from 'react'
import {
  startViva,
  runVivaTurn,
  rerunVivaTurn,
  listVivaSessions,
  getVivaSession,
  deleteVivaSession,
  forkVivaSession,
  getVivaPatient,
  createVivaPatient,
  placeVivaOrder,
  getVivaProvenance,
  getVivaPatientLiveState,
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
  UserCircleIcon,
  PencilIcon,
  XMarkIcon,
  CheckIcon,
  ExclamationTriangleIcon,
  QuestionMarkCircleIcon,
  CircleStackIcon,
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
const ORDER_TYPE_COLOR = {
  med:        'text-blue-300 bg-blue-400/10 border-blue-400/20',
  medication: 'text-blue-300 bg-blue-400/10 border-blue-400/20',
  lab:        'text-purple-300 bg-purple-400/10 border-purple-400/20',
  procedure:  'text-teal-300 bg-teal-400/10 border-teal-400/20',
  monitoring: 'text-teal-300 bg-teal-400/10 border-teal-400/20',
  comm:       'text-orange-300 bg-orange-400/10 border-orange-400/20',
  vents:      'text-cyan-300 bg-cyan-400/10 border-cyan-400/20',
  diet:       'text-green-300 bg-green-400/10 border-green-400/20',
  blood:      'text-red-300 bg-red-400/10 border-red-400/20',
}
const CONFIDENCE_COLOR = {
  high:   'text-green-400',
  medium: 'text-amber-400',
  low:    'text-red-400',
}
const ACTION_COLOR = {
  new:  'text-green-400 bg-green-400/10',
  edit: 'text-amber-400 bg-amber-400/10',
  stop: 'text-red-400 bg-red-400/10',
}

// Returns true for values that should not be displayed (not applicable)
function isNA(val) {
  if (!val) return true
  const s = String(val).trim().toLowerCase()
  return s === 'n/a' || s === 'na' || s === '—' || s === '-' || s === 'none' || s === 'null'
}

function badge(label, colorClass) {
  return (
    <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded uppercase tracking-wider ${colorClass}`}>
      {label}
    </span>
  )
}

// ── Provenance panel ───────────────────────────────────────────────────────────

function WhyPanel({ order, orderRunId, onClose }) {
  const [trace, setTrace] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!orderRunId) { setLoading(false); return }
    getVivaProvenance(orderRunId)
      .then(d => { setTrace(d); setLoading(false) })
      .catch(e => { setError(e.message); setLoading(false) })
  }, [orderRunId])

  // Match trace entries to this specific order by drug/orderable name
  const needle = (order.orderable_name || '').toLowerCase().split(' ')[0]

  const matchedGroundings = (trace?.chat_trace?.step2?.retrievals || []).filter(r =>
    needle && (
      r.parameter.toLowerCase().includes(needle) ||
      (r.search_query || '').toLowerCase().includes(needle)
    )
  )

  const matchedSearches = (trace?.order_trace?.phase1?.iterations || [])
    .flatMap(it => it.tool_calls || [])
    .filter(tc => tc.name === 'search_orderables' && needle &&
      (tc.args?.query || '').toLowerCase().includes(needle)
    )

  const weightRes = trace?.order_trace?.weight_resolution
  const clinicalDirection = trace?.chat_trace?.step1?.clinical_direction || []

  const CONF = { high: 'text-green-400', medium: 'text-amber-400', low: 'text-red-400' }

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* backdrop */}
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />

      {/* panel */}
      <div className="relative w-[420px] h-full bg-ink-900 border-l border-border flex flex-col shadow-2xl">

        {/* header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border flex-shrink-0 bg-ink-800">
          <div className="flex items-center gap-2 min-w-0">
            <QuestionMarkCircleIcon className="w-4 h-4 text-accent flex-shrink-0" />
            <span className="text-xs font-semibold text-white truncate">
              {order.orderable_name || order.recommendation || 'Order'}
            </span>
            {order.confidence && (
              <span className={`text-[10px] font-mono flex-shrink-0 ${CONF[order.confidence] || 'text-muted'}`}>
                {order.confidence}
              </span>
            )}
          </div>
          <button onClick={onClose} className="p-1 rounded text-muted hover:text-white flex-shrink-0">
            <XMarkIcon className="w-4 h-4" />
          </button>
        </div>

        {/* body */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5 text-xs">

          {loading && (
            <p className="text-muted italic flex items-center gap-1.5">
              <ClockIcon className="w-3.5 h-3.5 animate-spin" /> Loading trace…
            </p>
          )}
          {error && <p className="text-red-400">Error: {error}</p>}
          {!loading && !trace && !error && (
            <p className="text-muted italic">No trace available — run this session again to capture provenance.</p>
          )}

          {/* 1 · Source recommendation */}
          <section>
            <p className="text-[9px] uppercase tracking-widest text-muted mb-1.5">Source Recommendation</p>
            <p className="text-white/80 italic leading-relaxed">{order.recommendation || '—'}</p>
          </section>

          {/* 2 · Clinical direction from reasoning model */}
          {clinicalDirection.length > 0 && (
            <section>
              <p className="text-[9px] uppercase tracking-widest text-muted mb-1.5">Clinical Direction (reasoning model)</p>
              <ul className="space-y-1">
                {clinicalDirection.map((d, i) => (
                  <li key={i} className="flex gap-1.5 text-white/70 leading-relaxed">
                    <span className="text-accent flex-shrink-0 mt-0.5">→</span>
                    <span>{d}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* 3 · Wiki grounding */}
          {matchedGroundings.length > 0 && (
            <section>
              <p className="text-[9px] uppercase tracking-widest text-muted mb-1.5">Wiki Grounding</p>
              <div className="space-y-2">
                {matchedGroundings.map((g, i) => (
                  <div key={i} className="bg-ink-800 rounded-lg p-2.5 space-y-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-white/60 truncate">{g.parameter}</span>
                      <span className={`font-mono flex-shrink-0 ${g.grounded ? 'text-green-400' : 'text-red-400'}`}>
                        {g.grounded ? '✓ grounded' : '✗ not found'}
                      </span>
                    </div>
                    {g.value && (
                      <p className="font-mono text-accent">{g.value}</p>
                    )}
                    {g.source && (
                      <p className="text-[10px] text-muted font-mono">
                        {g.source}
                        {g.top_score != null && <span className="ml-1 opacity-60">(score {g.top_score.toFixed(2)})</span>}
                      </p>
                    )}
                    {!g.grounded && g.resolution_question && (
                      <p className="text-[10px] text-amber-400/80 italic">{g.resolution_question}</p>
                    )}
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* 4 · Catalog search */}
          {matchedSearches.length > 0 && (
            <section>
              <p className="text-[9px] uppercase tracking-widest text-muted mb-1.5">Catalog Search</p>
              <div className="space-y-1.5">
                {matchedSearches.map((tc, i) => (
                  <div key={i} className="bg-ink-800 rounded-lg p-2.5 space-y-1">
                    <p className="text-muted font-mono">
                      query: <span className="text-white">{tc.args?.query}</span>
                    </p>
                    {Array.isArray(tc.result_summary) && (
                      <p className="text-muted">
                        matches:{' '}
                        <span className="text-white">
                          {tc.result_summary.length ? tc.result_summary.join(', ') : 'none'}
                        </span>
                      </p>
                    )}
                    {tc.result_count != null && (
                      <p className="text-[10px] text-muted font-mono">{tc.result_count} result{tc.result_count !== 1 ? 's' : ''}</p>
                    )}
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* 5 · Weight resolution */}
          {weightRes && (
            <section>
              <p className="text-[9px] uppercase tracking-widest text-muted mb-1.5">Weight Used</p>
              <div className="bg-ink-800 rounded-lg p-2.5 space-y-1">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-white">
                    {weightRes.weight_kg != null ? `${weightRes.weight_kg} kg` : 'unavailable'}
                  </span>
                  <span className={`font-mono ${
                    weightRes.source === 'actual_emr' ? 'text-green-400' :
                    weightRes.source === 'none' ? 'text-red-400' : 'text-amber-400'
                  }`}>
                    {weightRes.source?.replace(/_/g, ' ')}
                  </span>
                </div>
                {weightRes.note && (
                  <p className="text-[10px] text-muted leading-relaxed">{weightRes.note}</p>
                )}
                {order.dose_calculation && (
                  <p className="font-mono text-accent/80 text-[10px]">{order.dose_calculation}</p>
                )}
              </div>
            </section>
          )}

        </div>

        {/* footer with run ids */}
        {trace && (
          <div className="px-4 py-2 border-t border-border bg-ink-800/50 flex-shrink-0 space-y-0.5">
            <p className="text-[9px] text-muted font-mono truncate">
              order run: {trace.order_trace?.run_id || '—'}
            </p>
            {trace.chat_trace && (
              <p className="text-[9px] text-muted font-mono truncate">
                chat run: {trace.chat_trace.run_id || '—'}
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Order card ─────────────────────────────────────────────────────────────────

function OrderCard({ order, onPlace, onIgnore, onWhy }) {
  const [status, setStatus] = useState('pending') // pending | placed | ignored
  const [editing, setEditing] = useState(false)
  const [editedDetails, setEditedDetails] = useState(order.order_details || {})
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)

  const typeKey = (order.order_type || 'med').toLowerCase()
  const typeColor = ORDER_TYPE_COLOR[typeKey] || 'text-muted bg-ink-700 border-border'
  const confColor = CONFIDENCE_COLOR[order.confidence] || 'text-muted'
  const action = (order.action || 'new').toLowerCase()
  const actionColor = ACTION_COLOR[action] || ACTION_COLOR.new

  async function handlePlace() {
    setLoading(true)
    setErr(null)
    try {
      await onPlace({ ...order, order_details: editedDetails })
      setStatus('placed')
      setEditing(false)
    } catch (e) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }

  function handleIgnore() {
    setStatus('ignored')
    onIgnore(order)
  }

  const isAcknowledged = status === 'placed' || status === 'ignored'

  return (
    <div className={`border rounded-lg overflow-hidden transition-opacity ${
      isAcknowledged ? 'opacity-50' : ''
    } ${typeColor.includes('blue') ? 'border-blue-400/20' : typeColor.includes('purple') ? 'border-purple-400/20' : typeColor.includes('teal') ? 'border-teal-400/20' : typeColor.includes('orange') ? 'border-orange-400/20' : typeColor.includes('cyan') ? 'border-cyan-400/20' : 'border-border'}`}>

      {/* Header row */}
      <div className="flex items-center justify-between px-3 py-2 bg-ink-800">
        <div className="flex items-center gap-2 min-w-0">
          <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded uppercase tracking-wider border ${typeColor}`}>
            {typeKey}
          </span>
          {action !== 'new' && (
            <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded uppercase tracking-wider ${actionColor}`}>
              {action}
            </span>
          )}
          <span className="text-xs text-white font-medium truncate">
            {order.orderable_name || order.recommendation || '—'}
          </span>
        </div>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          {order.confidence && (
            <span className={`text-[10px] font-mono ${confColor}`}>{order.confidence}</span>
          )}
          {onWhy && (
            <button
              onClick={() => onWhy(order)}
              title="Show provenance"
              className="p-0.5 rounded text-muted hover:text-accent transition-colors"
            >
              <QuestionMarkCircleIcon className="w-3.5 h-3.5" />
            </button>
          )}
          {status === 'placed' && <CheckIcon className="w-4 h-4 text-green-400" />}
          {status === 'ignored' && <XMarkIcon className="w-4 h-4 text-muted" />}
        </div>
      </div>

      {/* Body */}
      {!isAcknowledged && (
        <div className="px-3 py-2 space-y-2 bg-ink-900/50">
          {/* Edit/Stop dose transition — only show if values differ */}
          {action === 'edit' && order.from_dose && order.to_dose &&
            order.from_dose.trim().toLowerCase() !== order.to_dose.trim().toLowerCase() && (
            <div className="flex items-center gap-2 text-sm">
              <span className="text-red-400/70 line-through">{order.from_dose}</span>
              <span className="text-muted">→</span>
              <span className="text-green-400">{order.to_dose}</span>
            </div>
          )}
          {action === 'stop' && (
            <p className="text-sm text-red-400">Discontinue this order</p>
          )}

          {/* Order details grid */}
          {action !== 'stop' && (
            editing ? (
              <div className="grid grid-cols-2 gap-1.5">
                {['quantity', 'unit', 'route', 'form', 'frequency'].map(field => (
                  <div key={field}>
                    <label className="text-[9px] text-muted uppercase tracking-wider">{field}</label>
                    <input
                      value={editedDetails[field] || ''}
                      onChange={e => setEditedDetails(d => ({ ...d, [field]: e.target.value }))}
                      className="w-full bg-ink-700 border border-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-accent"
                    />
                  </div>
                ))}
                <div className="col-span-2">
                  <label className="text-[9px] text-muted uppercase tracking-wider">instructions</label>
                  <input
                    value={editedDetails.instructions || ''}
                    onChange={e => setEditedDetails(d => ({ ...d, instructions: e.target.value }))}
                    className="w-full bg-ink-700 border border-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-accent"
                  />
                </div>
              </div>
            ) : (
              <div className="grid grid-cols-3 gap-x-3 gap-y-0.5 text-xs">
                {!isNA(editedDetails.quantity) && (
                  <span><span className="text-muted">dose </span><span className="text-white">{editedDetails.quantity}{!isNA(editedDetails.unit) ? ` ${editedDetails.unit}` : ''}</span></span>
                )}
                {!isNA(editedDetails.route) && (
                  <span><span className="text-muted">route </span><span className="text-white">{editedDetails.route}</span></span>
                )}
                {!isNA(editedDetails.frequency) && (
                  <span><span className="text-muted">freq </span><span className="text-white">{editedDetails.frequency}</span></span>
                )}
                {!isNA(editedDetails.form) && (
                  <span><span className="text-muted">form </span><span className="text-white">{editedDetails.form}</span></span>
                )}
                {!isNA(editedDetails.instructions) && (
                  <span className="col-span-3 text-white/70 italic text-[11px]">{editedDetails.instructions}</span>
                )}
              </div>
            )
          )}

          {order.dose_calculation && (
            <p className="text-[11px] font-mono text-muted">{order.dose_calculation}</p>
          )}
          {order.notes && (
            <p className="text-[11px] text-amber-400/80 italic">{order.notes}</p>
          )}
          {err && <p className="text-[11px] text-red-400">{err}</p>}

          {/* Action buttons */}
          <div className="flex items-center gap-2 pt-1">
            <button
              onClick={handlePlace}
              disabled={loading}
              className="flex items-center gap-1 bg-accent/90 text-black text-xs font-medium px-2.5 py-1 rounded hover:bg-accent disabled:opacity-40 transition-colors"
            >
              {loading ? <ClockIcon className="w-3 h-3 animate-spin" /> : <CheckIcon className="w-3 h-3" />}
              {action === 'edit' ? 'Update' : action === 'stop' ? 'Stop' : 'Place'}
            </button>
            {action !== 'stop' && (
              <button
                onClick={() => setEditing(e => !e)}
                className="flex items-center gap-1 text-xs text-muted hover:text-white border border-border hover:border-accent/50 px-2.5 py-1 rounded transition-colors"
              >
                <PencilIcon className="w-3 h-3" />
                Edit
              </button>
            )}
            <button
              onClick={handleIgnore}
              className="flex items-center gap-1 text-xs text-muted hover:text-white px-2 py-1 rounded transition-colors"
            >
              <XMarkIcon className="w-3 h-3" />
              Ignore
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Instructions card (low-confidence orders grouped together) ─────────────────

function InstructionsCard({ orders, onAcknowledge }) {
  const [dismissed, setDismissed] = useState(false)

  function handleDismiss() {
    setDismissed(true)
    onAcknowledge()
  }

  if (dismissed) return null

  return (
    <div className="border border-yellow-500/20 rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-3 py-2 bg-ink-800">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono px-1.5 py-0.5 rounded uppercase tracking-wider border text-yellow-300 bg-yellow-400/10 border-yellow-400/20">
            instructions
          </span>
          <span className="text-xs text-white/60 font-medium">Low confidence — review manually</span>
        </div>
        <span className="text-[10px] text-muted font-mono">{orders.length} item{orders.length !== 1 ? 's' : ''}</span>
      </div>
      <div className="px-3 py-2 bg-ink-900/50 space-y-1.5">
        {orders.map((o, i) => (
          <div key={i} className="flex items-start gap-2 text-xs">
            <span className="text-muted mt-0.5 flex-shrink-0">•</span>
            <div className="space-y-0.5">
              <span className="text-white/80">{o.recommendation || o.orderable_name || '—'}</span>
              {o.notes && <p className="text-[11px] text-amber-400/70 italic">{o.notes}</p>}
            </div>
          </div>
        ))}
        <div className="pt-1.5">
          <button
            onClick={handleDismiss}
            className="flex items-center gap-1 text-xs text-muted hover:text-white px-2 py-1 rounded border border-border hover:border-accent/50 transition-colors"
          >
            <CheckIcon className="w-3 h-3" />
            Acknowledge
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Turn card ──────────────────────────────────────────────────────────────────

function TurnCard({ turn, defaultOpen = false, onAllAcknowledged, onWhy, onRerun, rerunning = false }) {
  const [open, setOpen] = useState(defaultOpen)
  const [acknowledged, setAcknowledged] = useState({})
  const scenario = turn.scenario || {}
  const snap = turn.student_snap || {}
  const orders = turn.orders || []

  // Split by confidence: high/medium → actionable order cards; low → instructions card
  const actionable = orders.map((o, i) => ({ ...o, _idx: i })).filter(o => (o.confidence || 'medium') !== 'low')
  const instructions = orders.filter(o => (o.confidence || 'medium') === 'low')
  const hasInstructions = instructions.length > 0

  const allAcknowledged =
    (actionable.length === 0 || actionable.every(o => acknowledged[o._idx])) &&
    (!hasInstructions || acknowledged['instructions'])

  const pendingCount =
    actionable.filter(o => !acknowledged[o._idx]).length +
    (hasInstructions && !acknowledged['instructions'] ? 1 : 0)

  useEffect(() => {
    if (allAcknowledged && onAllAcknowledged) {
      onAllAcknowledged()
    }
  }, [allAcknowledged])

  async function handlePlace(order) {
    await placeVivaOrder(order)
  }

  function markAck(key) {
    setAcknowledged(a => ({ ...a, [key]: true }))
  }

  const totalOrderCount = orders.length

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      {/* Header — div instead of button to allow nested action buttons */}
      <div
        onClick={() => setOpen(o => !o)}
        role="button"
        tabIndex={0}
        onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && setOpen(o => !o)}
        className="w-full flex items-center justify-between px-4 py-3 bg-ink-800 hover:bg-ink-700 transition-colors cursor-pointer select-none"
      >
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-muted">Turn {turn.turn_num}</span>
          {scenario.phase && badge(scenario.phase, PHASE_COLOR[scenario.phase] || 'text-muted bg-ink-700')}
          {scenario.difficulty && badge(scenario.difficulty, DIFF_COLOR[scenario.difficulty] || 'text-muted bg-ink-700')}
          {totalOrderCount > 0 && (
            <span className={`text-[10px] font-mono ${allAcknowledged ? 'text-green-400' : 'text-amber-400'}`}>
              {allAcknowledged
                ? `${actionable.length} order${actionable.length !== 1 ? 's' : ''}${hasInstructions ? ` + ${instructions.length} instr` : ''}`
                : `${pendingCount} pending`}
            </span>
          )}
          {turn.gaps_resolved > 0 && (
            <span className="text-[10px] text-teal-400 font-mono">
              +{turn.gaps_resolved} gap{turn.gaps_resolved !== 1 ? 's' : ''}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {turn.rerun && (
            <span className="text-[9px] font-mono text-purple-400/70 uppercase tracking-wider">rerun</span>
          )}
          {turn.cost_usd != null && (
            <span className="text-[10px] text-muted font-mono">${turn.cost_usd.toFixed(4)}</span>
          )}
          {onRerun && (
            <button
              onClick={e => { e.stopPropagation(); onRerun(turn.turn_num) }}
              disabled={rerunning}
              title={`Rerun Turn ${turn.turn_num}`}
              className="p-1 rounded text-muted hover:text-amber-400 disabled:opacity-40 transition-colors"
            >
              {rerunning
                ? <ClockIcon className="w-3.5 h-3.5 animate-spin" />
                : <ArrowPathIcon className="w-3.5 h-3.5" />}
            </button>
          )}
          {open ? <ChevronDownIcon className="w-4 h-4 text-muted" /> : <ChevronRightIcon className="w-4 h-4 text-muted" />}
        </div>
      </div>

      {open && (
        <div className="divide-y divide-border">
          {/* Clinical context */}
          <div className="px-4 py-3 bg-ink-900/50">
            <p className="text-[10px] uppercase tracking-widest text-muted mb-2">Clinical Context</p>
            <p className="text-sm text-white leading-relaxed">{scenario.clinical_context}</p>
            {scenario.question && (
              <p className="mt-2 text-sm text-accent font-medium italic">{scenario.question}</p>
            )}
          </div>

          {/* Orders section */}
          <div className="px-4 py-3">
            <div className="flex items-center justify-between mb-3">
              <p className="text-[10px] uppercase tracking-widest text-muted">Orders</p>
              {totalOrderCount > 0 && !allAcknowledged && (
                <span className="flex items-center gap-1 text-[10px] text-amber-400">
                  <ExclamationTriangleIcon className="w-3 h-3" />
                  {pendingCount} unacknowledged
                </span>
              )}
              {totalOrderCount > 0 && allAcknowledged && (
                <span className="flex items-center gap-1 text-[10px] text-green-400">
                  <CheckCircleIcon className="w-3 h-3" />
                  all acknowledged
                </span>
              )}
            </div>

            {totalOrderCount === 0 ? (
              <p className="text-sm text-muted italic">No orders generated for this turn.</p>
            ) : (
              <div className="space-y-2">
                {/* High / medium confidence → individual order cards */}
                {actionable.map((order) => (
                  <OrderCard
                    key={order._idx}
                    order={order}
                    onPlace={async (o) => { await handlePlace(o); markAck(order._idx) }}
                    onIgnore={() => markAck(order._idx)}
                    onWhy={onWhy ? (o) => onWhy(o, turn.order_run_id) : null}
                  />
                ))}
                {/* Low confidence → single instructions card */}
                {hasInstructions && (
                  <InstructionsCard
                    orders={instructions}
                    onAcknowledge={() => markAck('instructions')}
                  />
                )}
              </div>
            )}
          </div>

          {/* Reasoning */}
          {snap.pages_consulted?.length > 0 && (
            <div className="px-4 py-2">
              <p className="text-[10px] text-muted">
                Wiki: {snap.pages_consulted.join(', ')}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Patient chart drawer — tab sub-components ──────────────────────────────────

function VitalsTab({ vitals, historyCount }) {
  if (!vitals) return <p className="text-muted text-xs italic py-4">No vitals recorded yet.</p>

  const respSupport = vitals.isIntubated ? 'Intubated' : vitals.isNIV ? 'NIV' : vitals.isHFNC ? 'HFNC' : null

  const rows = [
    { label: 'SpO₂',  value: vitals.SpO2,        unit: '%',    alarm: v => v < 92 },
    { label: 'HR',    value: vitals.HR,           unit: 'bpm',  alarm: v => v > 100 || v < 60 },
    { label: 'RR',    value: vitals.RR,           unit: '/min', alarm: v => v > 20 },
    { label: 'BP',    value: vitals.BP,           unit: 'mmHg' },
    { label: 'MAP',   value: vitals.MAP,          unit: 'mmHg', alarm: v => v < 65 },
    { label: 'Temp',  value: vitals.Temperature,  unit: `°${vitals.TemperatureUnit || 'C'}`, alarm: v => v > 38.5 || v < 36 },
    { label: 'FiO₂',  value: vitals.FiO2 != null ? `${Math.round(vitals.FiO2 * 100)}%` : null },
    vitals.TherapyDevice && { label: 'Device', value: vitals.TherapyDevice },
    vitals.AVPU        && { label: 'AVPU',   value: vitals.AVPU },
    vitals.CVP != null && { label: 'CVP',    value: vitals.CVP, unit: 'cmH₂O' },
  ].filter(Boolean)

  const ventRows = (vitals.isIntubated || vitals.isNIV || vitals.isHFNC) ? [
    vitals.VentMode            && { label: 'Mode',   value: vitals.VentMode },
    vitals.VentPEEP != null    && { label: 'PEEP',   value: vitals.VentPEEP,  unit: 'cmH₂O' },
    vitals.VentPIP  != null    && { label: 'PIP',    value: vitals.VentPIP,   unit: 'cmH₂O' },
    vitals.VentRRSet != null   && { label: 'RR set', value: vitals.VentRRSet, unit: '/min' },
  ].filter(Boolean) : []

  return (
    <div className="space-y-3">
      {respSupport && (
        <span className="inline-block text-[10px] px-1.5 py-0.5 rounded font-mono bg-cyan-400/10 text-cyan-400 border border-cyan-400/20 uppercase tracking-wider">
          {respSupport}
        </span>
      )}

      <div className="grid grid-cols-2 gap-1.5">
        {rows.map(({ label, value, unit, alarm }) => {
          if (value == null) return null
          const numVal = typeof value === 'number' ? value : null
          const isAlarm = alarm && numVal != null && alarm(numVal)
          return (
            <div key={label} className="bg-ink-800 rounded p-2">
              <div className="text-[9px] text-muted uppercase tracking-wider">{label}</div>
              <div className={`text-sm font-mono font-medium ${isAlarm ? 'text-red-400' : 'text-white'}`}>
                {String(value)}{unit && <span className="text-[10px] text-muted ml-0.5">{unit}</span>}
              </div>
            </div>
          )
        })}
      </div>

      {ventRows.length > 0 && (
        <div>
          <p className="text-[9px] uppercase tracking-widest text-muted mb-1.5">Ventilator</p>
          <div className="grid grid-cols-2 gap-1.5">
            {ventRows.map(({ label, value, unit }) => (
              <div key={label} className="bg-ink-800 rounded p-2">
                <div className="text-[9px] text-muted uppercase tracking-wider">{label}</div>
                <div className="text-sm font-mono font-medium text-cyan-400">
                  {String(value)}{unit && <span className="text-[10px] text-muted ml-0.5">{unit}</span>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <p className="text-[9px] text-muted font-mono">
        {historyCount} snapshot{historyCount !== 1 ? 's' : ''}
        {vitals.timestamp && ` · ${new Date(vitals.timestamp).toLocaleTimeString()}`}
        {vitals.dataBy && ` · ${vitals.dataBy}`}
      </p>
    </div>
  )
}

function LabsTab({ labs }) {
  if (!labs || labs.length === 0) return <p className="text-muted text-xs italic py-4">No labs on file.</p>
  return (
    <div className="space-y-3">
      {labs.map((doc, i) => (
        <div key={i} className="bg-ink-800 rounded-lg overflow-hidden">
          <div className="flex items-center justify-between px-3 py-1.5 bg-ink-700">
            <span className="text-[10px] font-semibold text-white uppercase tracking-wider">{doc.name}</span>
            {doc.reportedAt && (
              <span className="text-[9px] text-muted font-mono">{new Date(doc.reportedAt).toLocaleTimeString()}</span>
            )}
          </div>
          {doc.text && Object.keys(doc.attributes || {}).length === 0 ? (
            <p className="px-3 py-2 text-xs text-white/80 leading-relaxed">{doc.text}</p>
          ) : (
            <div className="px-3 py-2 space-y-1">
              {Object.entries(doc.attributes || {}).map(([key, attr]) => {
                const val = attr.value
                const { min, max } = attr.normalRange || {}
                const numVal = parseFloat(val)
                const outOfRange = !isNaN(numVal) && min != null && max != null && (numVal < min || numVal > max)
                return (
                  <div key={key} className="flex items-center justify-between text-xs">
                    <span className="text-muted">{key}</span>
                    <div className="flex items-center gap-2">
                      <span className={`font-mono font-medium ${outOfRange ? 'text-red-400' : 'text-white'}`}>{val}</span>
                      {min != null && max != null && (
                        <span className="text-[9px] text-muted/60 font-mono">{min}–{max}</span>
                      )}
                    </div>
                  </div>
                )
              })}
              {doc.text && <p className="text-[10px] text-white/60 italic pt-1 leading-relaxed">{doc.text}</p>}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

function IOTab({ io }) {
  if (!io) return <p className="text-muted text-xs italic py-4">No IO data.</p>
  const balance = io.net_balance_ml ?? 0
  const balanceColor = balance > 2000 ? 'text-red-400' : balance > 500 ? 'text-amber-400' : 'text-white'
  const balanceStr = balance >= 0 ? `+${balance}` : String(balance)

  const rows = [
    { label: 'Urine output', value: `${io.total_urine_ml} mL` },
    { label: 'Urine rate',   value: `${io.urine_rate_ml_per_hr} mL/hr` },
    { label: 'Total intake', value: `${io.total_intake_ml} mL` },
    { label: 'Net balance',  value: `${balanceStr} mL`, color: balanceColor },
  ]

  return (
    <div className="space-y-2">
      <p className="text-[9px] text-muted uppercase tracking-widest">Last {io.period_hours} h</p>
      {rows.map(({ label, value, color }) => (
        <div key={label} className="flex items-center justify-between bg-ink-800 rounded px-3 py-2">
          <span className="text-xs text-muted">{label}</span>
          <span className={`text-sm font-mono font-medium ${color || 'text-white'}`}>{value}</span>
        </div>
      ))}
      <p className="text-[9px] text-muted font-mono">{io.entries_counted} IO entr{io.entries_counted === 1 ? 'y' : 'ies'} in period</p>
    </div>
  )
}

function OrdersTab({ orders }) {
  if (!orders) return <p className="text-muted text-xs italic py-4">No orders.</p>
  const sections = [
    { key: 'medications', label: 'Medications', color: 'text-blue-300' },
    { key: 'labs',        label: 'Labs',        color: 'text-purple-300' },
    { key: 'procedures',  label: 'Procedures',  color: 'text-teal-300' },
    { key: 'vents',       label: 'Ventilation', color: 'text-cyan-300' },
    { key: 'diets',       label: 'Diet',        color: 'text-green-300' },
    { key: 'bloods',      label: 'Blood',       color: 'text-red-300' },
  ]
  const hasAny = sections.some(s => (orders[s.key] || []).length > 0)
  if (!hasAny) return <p className="text-muted text-xs italic py-4">No active orders.</p>

  return (
    <div className="space-y-4">
      {sections.map(({ key, label, color }) => {
        const items = orders[key] || []
        if (!items.length) return null
        return (
          <div key={key}>
            <p className={`text-[9px] uppercase tracking-widest mb-1.5 ${color}`}>{label} ({items.length})</p>
            <div className="space-y-1">
              {items.map((item, i) => (
                <div key={i} className="bg-ink-800 rounded px-3 py-2">
                  <p className="text-xs text-white font-medium">
                    {item.name || item.investigation || item.pType || '—'}
                  </p>
                  <p className="text-[10px] text-muted font-mono mt-0.5">
                    {[
                      !isNA(item.quantity) && `${item.quantity}${!isNA(item.unit) ? ` ${item.unit}` : ''}`.trim(),
                      !isNA(item.route) && item.route,
                      !isNA(item.frequency) && item.frequency,
                      !isNA(item.discipline) && item.discipline,
                    ].filter(Boolean).join(' · ')}
                    {!isNA(item.instructions) && ` — ${item.instructions}`}
                  </p>
                </div>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function NotesTab({ notes }) {
  if (!notes || notes.length === 0) return <p className="text-muted text-xs italic py-4">No notes on file.</p>
  const CAT_COLOR = { event: 'text-amber-400', nursing: 'text-blue-400', ecg: 'text-purple-400', imaging: 'text-teal-400' }
  return (
    <div className="space-y-3">
      {notes.map((note, i) => (
        <div key={i} className="bg-ink-800 rounded-lg px-3 py-2.5 space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className={`text-[9px] font-mono uppercase tracking-wider ${CAT_COLOR[note.category] || 'text-muted'}`}>
              {note.category}{note.name && note.name !== 'Event Note' ? ` · ${note.name}` : ''}
            </span>
            {note.reportedAt && (
              <span className="text-[9px] text-muted font-mono flex-shrink-0">
                {new Date(note.reportedAt).toLocaleTimeString()}
              </span>
            )}
          </div>
          <p className="text-xs text-white/80 leading-relaxed">{note.text}</p>
        </div>
      ))}
    </div>
  )
}

function HistoryTab({ history }) {
  if (!history) return <p className="text-muted text-xs italic py-4">No history data.</p>
  const { home_medications = [], diagnoses = [], allergies = [] } = history
  const Section = ({ label, items, color = 'text-white/80' }) => (
    <div className="bg-ink-800 rounded-lg px-3 py-2.5 space-y-1.5">
      <span className="text-[9px] font-mono uppercase tracking-wider text-muted">{label}</span>
      {items.length === 0
        ? <p className="text-xs text-muted italic">None recorded</p>
        : <ul className="space-y-0.5">{items.map((item, i) => (
            <li key={i} className={`text-xs ${color}`}>{item}</li>
          ))}</ul>
      }
    </div>
  )
  return (
    <div className="space-y-3">
      <Section label="Home Medications" items={home_medications} color="text-emerald-300" />
      <Section label="Diagnoses / PMHx" items={diagnoses} />
      <Section label="Allergies" items={allergies} color="text-red-400" />
    </div>
  )
}

// ── Patient chart drawer ───────────────────────────────────────────────────────

const CHART_TABS = ['vitals', 'labs', 'io', 'orders', 'notes', 'hx']

function PatientChartDrawer({ onClose, refreshTrigger }) {
  const [tab, setTab] = useState('vitals')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [lastRefresh, setLastRefresh] = useState(null)

  async function refresh() {
    setLoading(true)
    setError(null)
    try {
      const d = await getVivaPatientLiveState()
      setData(d)
      setLastRefresh(new Date())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [])
  useEffect(() => { if (refreshTrigger > 0) refresh() }, [refreshTrigger])

  return (
    <div className="fixed right-0 top-0 h-full w-96 bg-ink-900 border-l border-border z-40 flex flex-col shadow-2xl">

      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-ink-800 flex-shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <CircleStackIcon className="w-4 h-4 text-accent flex-shrink-0" />
          <span className="text-xs font-semibold text-white">Patient Chart</span>
          {lastRefresh && (
            <span className="text-[9px] text-muted font-mono">{lastRefresh.toLocaleTimeString()}</span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={refresh}
            disabled={loading}
            title="Refresh from MongoDB"
            className="p-1.5 rounded text-muted hover:text-white disabled:opacity-40 transition-colors"
          >
            <ArrowPathIcon className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button onClick={onClose} className="p-1.5 rounded text-muted hover:text-white transition-colors">
            <XMarkIcon className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Tab bar */}
      <div className="flex border-b border-border flex-shrink-0 bg-ink-800/50">
        {CHART_TABS.map(t => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 text-[10px] uppercase tracking-wider py-2 transition-colors ${
              tab === t
                ? 'text-accent border-b-2 border-accent -mb-px'
                : 'text-muted hover:text-white'
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-4 py-3">
        {loading && !data ? (
          <div className="flex items-center gap-2 text-muted text-xs py-8 justify-center">
            <ClockIcon className="w-4 h-4 animate-spin" /> Loading…
          </div>
        ) : error ? (
          <p className="text-red-400 text-xs py-4">{error}</p>
        ) : !data ? (
          <p className="text-muted text-xs italic py-4">No patient found. Create a dummy patient first.</p>
        ) : (
          <>
            {tab === 'vitals'  && <VitalsTab  vitals={data.vitals} historyCount={data.vitals_history_count} />}
            {tab === 'labs'    && <LabsTab    labs={data.labs} />}
            {tab === 'io'      && <IOTab      io={data.io} />}
            {tab === 'orders'  && <OrdersTab  orders={data.orders} />}
            {tab === 'notes'   && <NotesTab   notes={data.notes} />}
            {tab === 'hx'      && <HistoryTab history={data.history} />}
          </>
        )}
      </div>

      {/* Footer */}
      <div className="px-4 py-2 border-t border-border bg-ink-800/50 flex-shrink-0">
        <p className="text-[9px] text-muted font-mono">MongoDB · VIVA_DUMMY_001</p>
      </div>
    </div>
  )
}

// ── Dummy patient panel ────────────────────────────────────────────────────────

const DEFAULT_PATIENT_FORM = {
  name: 'Viva Patient',
  age_years: 50,
  gender: 'male',
  weight_kg: 70,
  height_cm: 170,
  creatinine: 90,
  egfr: 75,
  allergies: '',
  diagnoses: '',
  home_meds: '',
}

function patientToForm(p) {
  if (!p) return DEFAULT_PATIENT_FORM
  return {
    name: p.name || 'Viva Patient',
    age_years: p.age_years ?? 50,
    gender: p.gender || 'male',
    weight_kg: p.weight_kg ?? 70,
    height_cm: p.height_cm ?? 170,
    creatinine: p.creatinine ?? 90,
    egfr: p.egfr ?? 75,
    allergies: (p.allergies || []).join(', '),
    diagnoses: (p.diagnoses || []).join(', '),
    home_meds: (p.home_meds || []).join('\n'),
  }
}

function PatientPanel({ patient, onSaved }) {
  const [open, setOpen] = useState(!patient)
  const [isNew, setIsNew] = useState(false)
  const [form, setForm] = useState(patientToForm(patient))
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)

  function field(label, key, type = 'text', placeholder = '') {
    return (
      <div>
        <label className="block text-[9px] text-muted uppercase tracking-wider mb-0.5">{label}</label>
        <input
          type={type}
          value={form[key]}
          placeholder={placeholder}
          onChange={e => setForm(f => ({ ...f, [key]: e.target.value }))}
          className="w-full bg-ink-700 border border-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-accent"
        />
      </div>
    )
  }

  async function handleSave() {
    setLoading(true)
    setErr(null)
    try {
      const payload = {
        name: form.name || 'Viva Patient',
        age_years: Number(form.age_years) || 50,
        gender: form.gender,
        weight_kg: form.weight_kg !== '' ? Number(form.weight_kg) : null,
        height_cm: form.height_cm !== '' ? Number(form.height_cm) : null,
        creatinine: form.creatinine !== '' ? Number(form.creatinine) : null,
        egfr: form.egfr !== '' ? Number(form.egfr) : null,
        allergies: form.allergies ? form.allergies.split(',').map(s => s.trim()).filter(Boolean) : [],
        diagnoses: form.diagnoses ? form.diagnoses.split(',').map(s => s.trim()).filter(Boolean) : [],
        home_meds: form.home_meds ? form.home_meds.split('\n').map(s => s.trim()).filter(Boolean) : [],
      }
      const data = await createVivaPatient(payload)
      onSaved(data.patient)
      setIsNew(false)
      setOpen(false)
    } catch (e) {
      setErr(e.message)
    } finally {
      setLoading(false)
    }
  }

  function handleNewPatient() {
    setForm(DEFAULT_PATIENT_FORM)
    setIsNew(true)
    setErr(null)
    setOpen(true)
  }

  return (
    <div className="border-b border-border">
      <div className="flex items-center">
        <button
          onClick={() => { setIsNew(false); setForm(patientToForm(patient)); setOpen(o => !o) }}
          className="flex-1 flex items-center justify-between px-4 py-2.5 hover:bg-ink-700 transition-colors"
        >
          <div className="flex items-center gap-2">
            <UserCircleIcon className="w-4 h-4 text-accent" />
            <span className="text-xs font-medium text-white">
              {isNew ? 'New Patient' : patient ? patient.name : 'Viva Patient'}
            </span>
          </div>
          <div className="flex items-center gap-1.5">
            {patient && !isNew && (
              <span className="text-[10px] text-muted font-mono">
                {patient.weight_kg ? `${patient.weight_kg}kg` : ''}
                {patient.weight_kg && patient.age_years ? ' · ' : ''}
                {patient.age_years ? `${patient.age_years}yr` : ''}
              </span>
            )}
            {open ? <ChevronDownIcon className="w-3.5 h-3.5 text-muted" /> : <ChevronRightIcon className="w-3.5 h-3.5 text-muted" />}
          </div>
        </button>
        <button
          onClick={handleNewPatient}
          title="Create new patient"
          className="px-2.5 py-2.5 text-muted hover:text-accent hover:bg-ink-700 transition-colors border-l border-border text-xs font-medium flex-shrink-0"
        >
          + New
        </button>
      </div>

      {open && (
        <div className="px-4 pb-4 space-y-2">
          <div className="grid grid-cols-2 gap-1.5">
            <div className="col-span-2">{field('Name', 'name', 'text', 'Viva Patient')}</div>
            {field('Age (yr)', 'age_years', 'number')}
            <div>
              <label className="block text-[9px] text-muted uppercase tracking-wider mb-0.5">Gender</label>
              <select
                value={form.gender}
                onChange={e => setForm(f => ({ ...f, gender: e.target.value }))}
                className="w-full bg-ink-700 border border-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-accent"
              >
                <option value="male">Male</option>
                <option value="female">Female</option>
              </select>
            </div>
            {field('Weight (kg)', 'weight_kg', 'number', 'e.g. 70')}
            {field('Height (cm)', 'height_cm', 'number', 'e.g. 170')}
            {field('Creatinine (µmol/L)', 'creatinine', 'number', 'e.g. 90')}
            {field('eGFR', 'egfr', 'number', 'e.g. 75')}
            <div className="col-span-2">{field('Allergies (comma-sep)', 'allergies', 'text', 'penicillin, NSAID')}</div>
            <div className="col-span-2">{field('Diagnoses (comma-sep)', 'diagnoses', 'text', 'T2DM, CKD')}</div>
            <div className="col-span-2">
              <label className="block text-[9px] text-muted uppercase tracking-wider mb-0.5">Home Medications (one per line)</label>
              <textarea
                value={form.home_meds}
                placeholder={'Lasix 40mg PO BID\nMetformin 500mg PO BD'}
                onChange={e => setForm(f => ({ ...f, home_meds: e.target.value }))}
                rows={3}
                className="w-full bg-ink-700 border border-border rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-accent resize-none"
              />
            </div>
          </div>
          {err && <p className="text-red-400 text-xs">{err}</p>}
          <button
            onClick={handleSave}
            disabled={loading}
            className="w-full flex items-center justify-center gap-1.5 bg-accent/90 text-black text-xs font-medium px-3 py-1.5 rounded hover:bg-accent disabled:opacity-40 transition-colors"
          >
            {loading ? <ClockIcon className="w-3 h-3 animate-spin" /> : <CheckIcon className="w-3 h-3" />}
            {isNew ? 'Create Patient' : 'Update Patient'}
          </button>
        </div>
      )}
    </div>
  )
}

// ── New viva form ──────────────────────────────────────────────────────────────

function NewVivaForm({ onStart, patient }) {
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

        {!patient && (
          <div className="flex items-center gap-2 p-3 rounded border border-amber-400/30 bg-amber-400/5">
            <ExclamationTriangleIcon className="w-4 h-4 text-amber-400 flex-shrink-0" />
            <p className="text-xs text-amber-400">
              No dummy patient configured. Set up a patient in the sidebar for weight-based dosing.
            </p>
          </div>
        )}

        {patient && (
          <div className="flex items-center gap-3 p-3 rounded border border-accent/20 bg-accent/5">
            <UserCircleIcon className="w-5 h-5 text-accent flex-shrink-0" />
            <div className="text-xs">
              <p className="text-white font-medium">{patient.name}</p>
              <p className="text-muted font-mono">
                {[patient.age_years && `${patient.age_years}yr`, patient.gender, patient.weight_kg && `${patient.weight_kg}kg`].filter(Boolean).join(' · ')}
              </p>
            </div>
          </div>
        )}

        <div>
          <label className="block text-xs text-muted mb-1.5 uppercase tracking-wider">Viva Topic</label>
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

        {error && <p className="text-red-400 text-sm">{error}</p>}

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

// ── Main page ──────────────────────────────────────────────────────────────────

export default function VivaSession() {
  const { activeKB } = useAppState()
  const [sessions, setSessions] = useState([])
  const [activeId, setActiveId] = useState(null)
  const [session, setSession] = useState(null)
  const [running, setRunning] = useState(false)
  const [rerunningTurn, setRerunningTurn] = useState(null) // turn_num being rerun
  const [error, setError] = useState(null)
  const [patient, setPatient] = useState(null)
  const [pendingTurnOrders, setPendingTurnOrders] = useState(false)
  const [provenance, setProvenance] = useState(null) // { order, orderRunId }
  const [chartOpen, setChartOpen] = useState(false)
  const [chartRefreshTrigger, setChartRefreshTrigger] = useState(0)
  const bottomRef = useRef(null)

  useEffect(() => {
    listVivaSessions(activeKB)
      .then(d => setSessions(d.sessions || []))
      .catch(() => {})
    getVivaPatient()
      .then(d => setPatient(d.patient || null))
      .catch(() => {})
  }, [activeKB])

  useEffect(() => {
    if (bottomRef.current) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [session?.turns?.length])

  // Reset pending flag when new turn completes
  useEffect(() => {
    const turns = session?.turns || []
    if (turns.length > 0) {
      const lastTurn = turns[turns.length - 1]
      if ((lastTurn.orders || []).length > 0) {
        setPendingTurnOrders(true)
      } else {
        setPendingTurnOrders(false)
      }
    }
  }, [session?.turns?.length])

  async function handleStart(topic, maxTurns, model) {
    const data = await startViva(topic, maxTurns, model, activeKB)
    const s = data.session
    setSession(s)
    setActiveId(s.session_id)
    setPendingTurnOrders(false)
    setSessions(prev => [
      { session_id: s.session_id, topic: s.topic, status: s.status, current_turn: s.current_turn, max_turns: s.max_turns, created_at: s.created_at, total_cost_usd: s.total_cost_usd, outcome: s.outcome },
      ...prev,
    ])
  }

  async function handleSelectSession(id) {
    setActiveId(id)
    setError(null)
    setPendingTurnOrders(false)
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
    setPendingTurnOrders(false)
    try {
      const data = await runVivaTurn(session.session_id, null, activeKB)
      setSession(data.session)
      setSessions(prev =>
        prev.map(s =>
          s.session_id === data.session.session_id
            ? { ...s, status: data.session.status, current_turn: data.session.current_turn, total_cost_usd: data.session.total_cost_usd, outcome: data.session.outcome }
            : s
        )
      )
      setChartRefreshTrigger(t => t + 1)
    } catch (e) {
      setError(e.message)
    } finally {
      setRunning(false)
    }
  }

  async function handleRerunTurn(turnNum) {
    if (!session || running || rerunningTurn != null) return
    if (!window.confirm(`Rerun Turn ${turnNum}? This will replace Turn ${turnNum} and all subsequent turns.`)) return
    setRerunningTurn(turnNum)
    setError(null)
    setPendingTurnOrders(false)
    try {
      const data = await rerunVivaTurn(session.session_id, turnNum, null, activeKB)
      setSession(data.session)
      setSessions(prev =>
        prev.map(s =>
          s.session_id === data.session.session_id
            ? { ...s, status: data.session.status, current_turn: data.session.current_turn, total_cost_usd: data.session.total_cost_usd, outcome: data.session.outcome }
            : s
        )
      )
    } catch (e) {
      setError(e.message)
    } finally {
      setRerunningTurn(null)
    }
  }

  async function handleFork(id) {
    try {
      const data = await forkVivaSession(id, activeKB)
      const s = data.session
      setSession(s)
      setActiveId(s.session_id)
      setPendingTurnOrders(false)
      setSessions(prev => [
        { session_id: s.session_id, topic: s.topic, status: s.status, current_turn: s.current_turn, max_turns: s.max_turns, created_at: s.created_at, total_cost_usd: s.total_cost_usd, outcome: s.outcome },
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
      if (activeId === id) { setActiveId(null); setSession(null) }
    } catch (e) {
      setError(e.message)
    }
  }

  const isComplete = session?.status === 'complete'
  const pendingScenario = session?.next_scenario
  const turns = session?.turns || []

  // Count total unacknowledged orders on the last turn
  const lastTurn = turns[turns.length - 1]
  const lastTurnOrderCount = (lastTurn?.orders || []).length

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

        {/* Dummy patient panel */}
        <PatientPanel patient={patient} onSaved={setPatient} />

        <div className="flex-1 overflow-y-auto py-2">
          <button
            onClick={() => { setActiveId(null); setSession(null); setError(null); setPendingTurnOrders(false) }}
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
          <NewVivaForm onStart={handleStart} patient={patient} />
        ) : session ? (
          <>
            {/* Session header */}
            <div className="px-6 py-4 border-b border-border flex items-center justify-between flex-shrink-0">
              <div>
                <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                  {session.topic}
                  {session.forked_from && badge('replay', 'text-purple-400 bg-purple-400/10')}
                </h2>
                <p className="text-xs text-muted mt-0.5 font-mono">
                  {session.session_id} · turn {session.current_turn}/{session.max_turns} · ${(session.total_cost_usd || 0).toFixed(4)}
                  {patient && (
                    <span className="ml-2 text-accent/70">
                      {patient.name}{patient.weight_kg ? ` · ${patient.weight_kg}kg` : ''}
                    </span>
                  )}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => setChartOpen(o => !o)}
                  title={chartOpen ? 'Close patient chart' : 'View live MongoDB patient chart'}
                  className={`flex items-center gap-1.5 text-xs border px-2.5 py-1.5 rounded transition-colors ${
                    chartOpen
                      ? 'text-accent border-accent/60 bg-accent/10'
                      : 'text-muted hover:text-white border-border hover:border-accent/50'
                  }`}
                >
                  <CircleStackIcon className="w-3.5 h-3.5" />
                  Chart
                </button>
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
                  onAllAcknowledged={i === turns.length - 1 ? () => setPendingTurnOrders(false) : undefined}
                  onWhy={(order, orderRunId) => setProvenance({ order, orderRunId })}
                  onRerun={handleRerunTurn}
                  rerunning={rerunningTurn === turn.turn_num}
                />
              ))}

              {isComplete && session.outcome && (
                <div className="border border-green-500/30 rounded-lg px-4 py-3 bg-green-500/5">
                  <p className="text-[10px] uppercase tracking-widest text-green-400 mb-1">Case Outcome</p>
                  <p className="text-sm text-white">{session.outcome}</p>
                </div>
              )}

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

              {error && <p className="text-red-400 text-sm px-1">{error}</p>}
              <div ref={bottomRef} />
            </div>

            {/* Run turn footer */}
            {!isComplete && (
              <div className="px-6 py-4 border-t border-border flex-shrink-0">
                {pendingTurnOrders && lastTurnOrderCount > 0 && (
                  <div className="flex items-center gap-2 mb-3 p-2.5 rounded border border-amber-400/30 bg-amber-400/5">
                    <ExclamationTriangleIcon className="w-4 h-4 text-amber-400 flex-shrink-0" />
                    <p className="text-xs text-amber-400">
                      Acknowledge all orders on Turn {lastTurn.turn_num} before running the next turn.
                    </p>
                  </div>
                )}
                <button
                  onClick={handleRunTurn}
                  disabled={running || pendingTurnOrders}
                  className="flex items-center gap-2 bg-accent text-black font-medium text-sm px-5 py-2.5 rounded hover:bg-accent/90 disabled:opacity-40 transition-colors"
                >
                  {running ? (
                    <><ClockIcon className="w-4 h-4 animate-spin" />Running turn {session.current_turn + 1}… (student → orders → gaps → teacher)</>
                  ) : (
                    <><PlayIcon className="w-4 h-4" />Run Turn {session.current_turn + 1}</>
                  )}
                </button>
                <p className="text-[10px] text-muted mt-1.5">
                  Each turn: student answers → orders generated → knowledge gaps resolved → teacher advances the case
                </p>
              </div>
            )}
          </>
        ) : null}
      </div>

      {provenance && (
        <WhyPanel
          order={provenance.order}
          orderRunId={provenance.orderRunId}
          onClose={() => setProvenance(null)}
        />
      )}

      {chartOpen && (
        <PatientChartDrawer
          onClose={() => setChartOpen(false)}
          refreshTrigger={chartRefreshTrigger}
        />
      )}
    </div>
  )
}
