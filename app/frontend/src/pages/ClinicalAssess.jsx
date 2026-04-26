import { useState, useEffect, useCallback } from 'react'
import {
  ChevronDownIcon,
  ChevronRightIcon,
  PlayIcon,
  ArrowTopRightOnSquareIcon,
} from '@heroicons/react/24/outline'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  runClinicalAssessment,
  clinicalAssessJobStatus,
  listClinicalAssessments,
  getClinicalAssessment,
  listAvailablePatients,
  listPatientSnapshots,
  rateSnapshotApi,
  saveRunComment,
  deleteClinicalAssessment,
  generateOrders,
} from '../api'
import CostBadge from '../components/CostBadge'
import { useAppState } from '../AppStateContext'

// ── Wiki link rendering (mirrors Chat.jsx) ────────────────────────────────────

const DEFAULT_VAULT = import.meta.env.VITE_VAULT_NAME || 'llm_wiki'
const WIKI_LINK_PREFIX = 'obsidian-wiki://'

function preprocessWikiLinks(text, vaultName = DEFAULT_VAULT) {
  if (!text) return ''
  return text.replace(/\[\[(.+?)\]\]/g, (_, title) => {
    const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
    const obsidianUrl = `obsidian://open?vault=${vaultName}&file=wiki/entities/${slug}`
    return `[${title}](${WIKI_LINK_PREFIX}${encodeURIComponent(obsidianUrl)})`
  })
}

const mdComponents = {
  a({ href, children }) {
    if (href?.startsWith(WIKI_LINK_PREFIX)) {
      const obsidianUrl = decodeURIComponent(href.slice(WIKI_LINK_PREFIX.length))
      return (
        <a href={obsidianUrl} target="_blank" rel="noreferrer"
          className="inline-flex items-center gap-1 px-2 py-0.5 mx-0.5 bg-accent/20 border border-accent/40 rounded text-accent text-xs font-mono hover:bg-accent/30 transition-colors">
          {children}
          <ArrowTopRightOnSquareIcon className="w-3 h-3 inline-block" />
        </a>
      )
    }
    return <a href={href} target="_blank" rel="noreferrer" className="text-accent underline underline-offset-2 hover:text-accent/80">{children}</a>
  },
  p({ children })      { return <p className="mb-2 last:mb-0 leading-relaxed">{children}</p> },
  ul({ children })     { return <ul className="list-disc list-outside ml-5 mb-2 space-y-1">{children}</ul> },
  ol({ children })     { return <ol className="list-decimal list-outside ml-5 mb-2 space-y-1">{children}</ol> },
  li({ children })     { return <li className="leading-relaxed">{children}</li> },
  strong({ children }) { return <strong className="font-semibold text-white">{children}</strong> },
}

// ── Accordion ─────────────────────────────────────────────────────────────────

function Accordion({ label, badge, badgeCls, borderCls, bgCls, labelCls, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className={`rounded-lg border ${borderCls} ${bgCls}`}>
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left"
      >
        {open
          ? <ChevronDownIcon className="w-3.5 h-3.5 text-muted flex-shrink-0" />
          : <ChevronRightIcon className="w-3.5 h-3.5 text-muted flex-shrink-0" />}
        <span className={`text-[9px] uppercase tracking-widest font-semibold ${labelCls}`}>{label}</span>
        {badge && (
          <span className={`text-[9px] font-mono px-1.5 py-0.5 rounded border ${badgeCls}`}>{badge}</span>
        )}
      </button>
      {open && <div className="px-3 pb-3">{children}</div>}
    </div>
  )
}

// ── Available models ──────────────────────────────────────────────────────────

const AVAILABLE_MODELS = [
  { value: '', label: 'Default (env)' },
  { value: 'claude-haiku-4-5', label: 'Claude Haiku 4.5' },
  { value: 'claude-sonnet-4-5', label: 'Claude Sonnet 4.5' },
  { value: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
  { value: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash' },
  { value: 'gemini-2.0-flash', label: 'Gemini 2.0 Flash' },
  { value: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
]

const REASONING_MODELS = [
  { value: '', label: 'Same as grounding model' },
  { value: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
  { value: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash' },
  { value: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
]

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

function TruncBadge({ text, className }) {
  if (!text) return null
  return (
    <span
      title={text}
      className={`inline-block max-w-[90px] truncate px-1.5 py-0.5 rounded border text-[9px] font-mono align-middle ${className}`}
    >
      {text}
    </span>
  )
}

function RunCard({ run, isSelected, onSelect, onDelete }) {
  const [deleting, setDeleting] = useState(false)

  const handleDelete = async (e) => {
    e.stopPropagation()
    setDeleting(true)
    try {
      await onDelete(run)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div
      onClick={() => onSelect(run)}
      className={`relative w-full text-left pl-7 pr-8 py-2 border-b border-border/50 transition-colors cursor-pointer group ${
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
      {/* Badges row */}
      <div className="flex flex-wrap gap-1 mt-1.5">
        <TruncBadge
          text={run.model}
          className="text-purple-400 bg-purple-950/30 border-purple-800/40"
        />
        {run.avg_rating != null && (
          <span
            title={`Average rating: ${run.avg_rating}/10`}
            className={`inline-flex items-center px-1.5 py-0.5 rounded border text-[9px] font-mono ${
              run.avg_rating >= 8 ? 'text-green-400 bg-green-950/30 border-green-800/40'
              : run.avg_rating >= 5 ? 'text-amber-400 bg-amber-950/30 border-amber-800/40'
              : 'text-red-400 bg-red-950/30 border-red-800/40'
            }`}
          >
            ★ {run.avg_rating}
          </span>
        )}
        <TruncBadge
          text={run.comment || null}
          className="text-amber-200/70 bg-amber-950/20 border-amber-800/30"
        />
      </div>
      {/* Delete button */}
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

function PatientGroup({ patientId, runs, selectedRunId, onSelect, onDelete }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-2 px-4 py-2 bg-ink-900 border-b border-border sticky top-0 z-10 hover:bg-ink-800 transition-colors text-left"
      >
        {open
          ? <ChevronDownIcon className="w-3 h-3 text-muted flex-shrink-0" />
          : <ChevronRightIcon className="w-3 h-3 text-muted flex-shrink-0" />}
        <span className="text-xs font-mono font-semibold text-white/80 flex-1">{patientId}</span>
        <span className="text-[10px] text-muted font-mono">{runs.length}</span>
      </button>
      {open && runs.map(r => (
        <RunCard
          key={r.run_id}
          run={r}
          isSelected={selectedRunId === r.run_id}
          onSelect={onSelect}
          onDelete={onDelete}
        />
      ))}
    </div>
  )
}

// ── Order card (mirrors OrderGenerator.jsx) ───────────────────────────────────

const ORDER_TYPE_COLORS = {
  med:        'bg-blue-500/15 text-blue-300 border-blue-500/30',
  lab:        'bg-purple-500/15 text-purple-300 border-purple-500/30',
  procedure:  'bg-amber-500/15 text-amber-300 border-amber-500/30',
  monitoring: 'bg-teal-500/15 text-teal-300 border-teal-500/30',
}
const CONF_COLORS = { high: 'text-green-400', medium: 'text-amber-400', low: 'text-red-400' }
const CONF_DOTS   = { high: 'bg-green-400',   medium: 'bg-amber-400',   low: 'bg-red-400'   }

function OrderCard({ order }) {
  const typeCls = ORDER_TYPE_COLORS[order.order_type] || ORDER_TYPE_COLORS.monitoring
  const confCls = CONF_COLORS[order.confidence] || CONF_COLORS.low
  const dotCls  = CONF_DOTS[order.confidence]   || CONF_DOTS.low
  const d = order.order_details || {}
  const rows = [
    d.name      && ['Drug / Item', d.name],
    d.quantity && d.unit && ['Dose', `${d.quantity} ${d.unit}`],
    d.route     && ['Route', d.route],
    d.form      && ['Form', d.form],
    d.frequency && ['Frequency', d.frequency],
    d.instructions && ['Instructions', d.instructions],
  ].filter(Boolean)

  return (
    <div className="border border-border rounded-lg p-3 space-y-2 bg-ink-900">
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs text-white/80 leading-snug flex-1">{order.recommendation}</p>
        <span className={`text-[9px] font-mono uppercase px-1.5 py-0.5 rounded border flex-shrink-0 ${typeCls}`}>
          {order.order_type}
        </span>
      </div>
      {order.orderable_name && (
        <p className="text-xs font-medium text-white">{order.orderable_name}</p>
      )}
      {rows.length > 0 && (
        <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[11px]">
          {rows.map(([label, value]) => (
            <>
              <span key={`l-${label}`} className="text-muted">{label}</span>
              <span key={`v-${label}`} className="text-white font-mono">{value}</span>
            </>
          ))}
        </div>
      )}
      {order.dose_calculation && (
        <p className="text-[11px] font-mono text-accent/80 bg-accent/5 px-2 py-1 rounded">
          {order.dose_calculation}
        </p>
      )}
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`flex items-center gap-1 text-[11px] ${confCls}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${dotCls}`} />
          {order.confidence} confidence
        </span>
        {order.notes && <span className="text-[11px] text-muted">· {order.notes}</span>}
      </div>
    </div>
  )
}

// ── Snapshot row ──────────────────────────────────────────────────────────────

function SnapshotRow({ snap, patientId, runId, activeKB, onRated, isOpen, onToggle }) {
  const [saving, setSaving] = useState(false)
  const [viewMode, setViewMode] = useState('rendered') // 'rendered' | 'raw' | 'orders'
  const [ordersResult, setOrdersResult] = useState(null)
  const [ordersLoading, setOrdersLoading] = useState(false)
  const [ordersError, setOrdersError] = useState(null)
  const [localRating, setLocalRating] = useState(snap.rating ?? null)
  const initGaps = Array.isArray(snap.knowledge_gaps)
    ? snap.knowledge_gaps
    : snap.knowledge_gaps ? [snap.knowledge_gaps] : []
  const [gaps, setGaps] = useState(initGaps)
  const [gapInput, setGapInput] = useState('')
  const [gapsSaving, setGapsSaving] = useState(false)
  const hasGapOnly = !!(snap.gap_entity && !snap.clinical_direction?.length && !snap.immediate_actions?.length && !snap.agent_answer)
  const hasGapAlso = !!(snap.gap_entity && (snap.clinical_direction?.length || snap.immediate_actions?.length || snap.agent_answer))
  const hasAnswer = !!(snap.agent_answer || snap.clinical_direction?.length || snap.immediate_actions?.length || snap.gap_entity)

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
        <span className={`flex-shrink-0 rounded-full bg-ink-700 border border-border text-xs font-mono text-muted flex items-center justify-center ${snap.snapshot_num === 0 ? 'px-2 h-6' : 'w-6 h-6'}`}>
          {snap.snapshot_num === 0 ? (snap.snapshot_dir?.replace('_', ' ') || 'demo') : snap.snapshot_num}
        </span>
        <div className="flex-1 flex items-center gap-2 flex-wrap">
          <PhaseBadge phase={snap.phase} />
          <DifficultyBadge difficulty={snap.difficulty} />
          {!hasAnswer && (
            <span className="text-xs text-muted italic">Not yet run</span>
          )}
          {hasGapOnly && (
            <span className="text-xs text-orange-400/80 bg-orange-950/20 border border-orange-800/40 rounded px-1.5 py-0.5 font-mono">
              KG registered
            </span>
          )}
          {hasGapAlso && (
            <span className="text-xs text-yellow-400/70 bg-yellow-950/20 border border-yellow-800/30 rounded px-1.5 py-0.5 font-mono">
              + KG filed
            </span>
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
          {/* Context: clinical context + timeline + question */}
          <div className="mb-5 space-y-3">
            {snap.clinical_context && (
              <div>
                <p className="text-[10px] uppercase tracking-widest text-muted mb-1">Clinical Context</p>
                <p className="text-xs text-white/70 bg-ink-900 border border-border rounded-lg p-3 leading-relaxed">
                  {snap.clinical_context}
                </p>
              </div>
            )}
            {snap.csv_content && (
              <div>
                <p className="text-[10px] uppercase tracking-widest text-muted mb-1">Timeline</p>
                <TimelineTable csv={snap.csv_content} />
              </div>
            )}
            {snap.question && (
              <div>
                <p className="text-[10px] uppercase tracking-widest text-muted mb-1">Question</p>
                <p className="text-xs text-white/80 italic">{snap.question}</p>
              </div>
            )}
          </div>

          {/* Clinical Summary — collapsed accordion */}
          {snap.timeline_summary && (
            <div className="mb-4">
              <Accordion
                label="Clinical Summary"
                labelCls="text-white/50"
                borderCls="border-border"
                bgCls="bg-ink-900"
                defaultOpen={false}
              >
                <div className="text-xs text-white/75 leading-relaxed prose-sm">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                    {snap.timeline_summary}
                  </ReactMarkdown>
                </div>
              </Accordion>
            </div>
          )}

          {!hasAnswer ? (
            <p className="text-sm text-muted italic">Run the assessment to get an answer.</p>
          ) : (
            <>
              {/* Gap-only response */}
              {hasGapOnly && (
                <div className="mb-4 rounded-lg border border-orange-800/40 bg-orange-950/20 px-4 py-3">
                  <p className="text-xs font-semibold text-orange-400 uppercase tracking-wide mb-1">
                    Knowledge Gap Registered
                  </p>
                  <p className="text-sm text-white/80">
                    The wiki does not have sufficient information to answer this question.
                    A knowledge gap has been registered for <span className="font-mono text-orange-300">{snap.gap_entity}</span> and will be resolved in the next learning cycle.
                  </p>
                  {snap.gap_sections?.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {snap.gap_sections.map((s, i) => (
                        <span key={i} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-orange-950/40 border border-orange-800/30 text-orange-300/80">{s}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Gap registered alongside an answer */}
              {hasGapAlso && (
                <div className="mb-4 rounded-lg border border-yellow-800/30 bg-yellow-950/10 px-4 py-3">
                  <p className="text-xs font-semibold text-yellow-400/80 uppercase tracking-wide mb-1">
                    Knowledge Gap Filed
                  </p>
                  <p className="text-xs text-white/70">
                    Wiki lacked complete information on <span className="font-mono text-yellow-300">{snap.gap_entity}</span>.
                    A gap was registered and will be resolved in the next learning cycle.
                  </p>
                  {snap.gap_sections?.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-2">
                      {snap.gap_sections.map((s, i) => (
                        <span key={i} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-yellow-950/40 border border-yellow-800/30 text-yellow-300/80">{s}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Side-by-side answers */}
              <div className="grid grid-cols-2 gap-4 mb-4">
                {/* Agent answer */}
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <p className="text-xs font-semibold text-accent uppercase tracking-wide">Agent Answer</p>
                    <div className="flex items-center gap-1">
                      {[
                        { key: 'rendered', label: 'Rendered' },
                        { key: 'raw',      label: 'Raw JSON' },
                        { key: 'orders',   label: 'Orders'   },
                      ].map(({ key, label }) => (
                        <button
                          key={key}
                          onClick={async () => {
                            setViewMode(key)
                            if (key === 'orders' && !ordersResult && !ordersLoading) {
                              const recs = [
                                ...(snap.immediate_actions || []),
                                ...(snap.monitoring_followup || []),
                              ].filter(Boolean)
                              if (!recs.length) return
                              const cpmrn = patientId.replace(/_\d+$/, '')
                              setOrdersLoading(true)
                              setOrdersError(null)
                              try {
                                const data = await generateOrders(
                                  { recommendations: recs, cpmrn, patientType: 'adult' },
                                  activeKB
                                )
                                setOrdersResult(data)
                              } catch (e) {
                                setOrdersError(e.message)
                              } finally {
                                setOrdersLoading(false)
                              }
                            }
                          }}
                          className={`text-[10px] border rounded px-1.5 py-0.5 transition-colors ${
                            viewMode === key
                              ? 'border-accent text-accent bg-accent/10'
                              : 'border-border text-muted hover:text-white/60'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  </div>
                  {viewMode === 'raw' ? (
                    <pre className="text-[11px] font-mono text-white/70 bg-ink-900 rounded-lg p-3 border border-border overflow-x-auto max-h-80 overflow-y-auto whitespace-pre-wrap">
                      {JSON.stringify({
                        clinical_direction: snap.clinical_direction,
                        specific_parameters: snap.specific_parameters,
                        clinical_reasoning: snap.agent_clinical_reasoning,
                        monitoring_followup: snap.monitoring_followup,
                        alternative_considerations: snap.alternative_considerations,
                        pages_consulted: snap.pages_consulted,
                        wiki_links: snap.wiki_links,
                        tokens_in: snap.tokens_in,
                        tokens_out: snap.tokens_out,
                      }, null, 2)}
                    </pre>
                  ) : viewMode === 'orders' ? (
                    <div className="space-y-2">
                      {ordersLoading && (
                        <p className="text-xs text-muted animate-pulse">Searching orderables catalog…</p>
                      )}
                      {ordersError && (
                        <p className="text-xs text-red-400">{ordersError}</p>
                      )}
                      {ordersResult?.weight_gap_registered && (
                        <div className="border border-amber-500/30 bg-amber-500/8 rounded px-3 py-2 text-[11px] text-amber-300">
                          <span className="font-medium">KG registered: </span>
                          <span className="font-mono">{ordersResult.weight_gap_registered}</span>
                        </div>
                      )}
                      {ordersResult?.orders?.map((order, i) => (
                        <OrderCard key={i} order={order} />
                      ))}
                      {ordersResult && !ordersResult.orders?.length && !ordersLoading && (
                        <p className="text-xs text-muted">No orders generated.</p>
                      )}
                    </div>
                  ) : (
                    <div className="space-y-2">

                      {/* Immediate Next Steps — open by default */}
                      {snap.immediate_next_steps?.length > 0 && (
                        <Accordion
                          label="Immediate Next Steps"
                          labelCls="text-emerald-400"
                          borderCls="border-emerald-800/50"
                          bgCls="bg-emerald-950/20"
                          defaultOpen={true}
                        >
                          <ol className="space-y-1.5 list-none">
                            {snap.immediate_next_steps.map((item, i) => (
                              <li key={i} className="flex gap-2.5 text-xs text-white/85 leading-snug">
                                <span className="flex-shrink-0 w-4 h-4 rounded-full bg-emerald-900/60 border border-emerald-700/50 text-emerald-400 text-[9px] font-bold flex items-center justify-center mt-0.5">
                                  {i + 1}
                                </span>
                                <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                                  {preprocessWikiLinks(item)}
                                </ReactMarkdown>
                              </li>
                            ))}
                          </ol>
                        </Accordion>
                      )}

                      {/* Monitoring & Follow-up — open by default */}
                      {snap.monitoring_followup?.length > 0 && (
                        <Accordion
                          label="Monitoring & Follow-up"
                          labelCls="text-blue-400"
                          borderCls="border-blue-800/50"
                          bgCls="bg-blue-950/20"
                          defaultOpen={true}
                        >
                          <ul className="space-y-1">
                            {snap.monitoring_followup.map((item, i) => (
                              <li key={i} className="flex gap-2 text-xs text-white/80 leading-snug">
                                <span className="text-blue-400 flex-shrink-0">›</span>
                                <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                                  {preprocessWikiLinks(item)}
                                </ReactMarkdown>
                              </li>
                            ))}
                          </ul>
                        </Accordion>
                      )}

                      {/* Clinical Direction — collapsed by default */}
                      {snap.clinical_direction?.length > 0 && (
                        <Accordion
                          label="Clinical Direction"
                          badge="reasoning"
                          badgeCls="bg-red-950/40 border-red-800/30 text-red-300/70"
                          labelCls="text-red-400"
                          borderCls="border-red-800/50"
                          bgCls="bg-red-950/20"
                          defaultOpen={false}
                        >
                          <ul className="space-y-1">
                            {snap.clinical_direction.map((item, i) => (
                              <li key={i} className="flex gap-2 text-xs text-white/80 leading-snug">
                                <span className="text-red-400 flex-shrink-0">›</span>
                                <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                                  {preprocessWikiLinks(item)}
                                </ReactMarkdown>
                              </li>
                            ))}
                          </ul>
                        </Accordion>
                      )}

                      {/* Specific Parameters — collapsed by default */}
                      {snap.specific_parameters?.length > 0 && (
                        <Accordion
                          label="Specific Parameters"
                          badge="wiki-grounded"
                          badgeCls="bg-cyan-950/40 border-cyan-800/30 text-cyan-300/70"
                          labelCls="text-cyan-400"
                          borderCls="border-cyan-800/40"
                          bgCls="bg-cyan-950/10"
                          defaultOpen={false}
                        >
                          <div className="space-y-1.5">
                            {snap.specific_parameters.map((p, i) => (
                              <div key={i} className={`flex items-start gap-2 rounded px-2 py-1.5 border ${
                                p.grounded
                                  ? 'border-green-800/30 bg-green-950/10'
                                  : 'border-orange-800/30 bg-orange-950/10'
                              }`}>
                                <span className={`flex-shrink-0 text-[9px] font-mono mt-0.5 px-1 py-0.5 rounded border ${
                                  p.grounded
                                    ? 'text-green-400 border-green-800/40 bg-green-950/30'
                                    : 'text-orange-400 border-orange-800/40 bg-orange-950/30'
                                }`}>
                                  {p.grounded ? '✓' : '?'}
                                </span>
                                <div className="flex-1 min-w-0">
                                  <span className="text-[10px] text-white/50 font-mono">{p.parameter}</span>
                                  {p.value && <p className="text-xs text-white/80 mt-0.5">{p.value}</p>}
                                  {!p.grounded && (
                                    <p className="text-[10px] text-orange-400/70 mt-0.5">
                                      {snap.gap_registered ? 'Not in wiki — gap registered' : 'Not in wiki'}
                                    </p>
                                  )}
                                  {p.source && <p className="text-[10px] text-white/30 font-mono mt-0.5">{p.source}</p>}
                                </div>
                              </div>
                            ))}
                          </div>
                        </Accordion>
                      )}

                      {/* Clinical Reasoning — collapsed by default */}
                      {snap.agent_clinical_reasoning?.length > 0 && (
                        <Accordion
                          label="Clinical Reasoning"
                          labelCls="text-amber-400"
                          borderCls="border-amber-800/50"
                          bgCls="bg-amber-950/20"
                          defaultOpen={false}
                        >
                          <ul className="space-y-1">
                            {snap.agent_clinical_reasoning.map((item, i) => (
                              <li key={i} className="flex gap-2 text-xs text-white/80 leading-snug">
                                <span className="text-amber-400 flex-shrink-0">›</span>
                                <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                                  {preprocessWikiLinks(item)}
                                </ReactMarkdown>
                              </li>
                            ))}
                          </ul>
                        </Accordion>
                      )}

                      {/* Alternative Considerations — collapsed by default */}
                      {snap.alternative_considerations?.length > 0 && (
                        <Accordion
                          label="Alternative Considerations"
                          labelCls="text-purple-400"
                          borderCls="border-purple-800/50"
                          bgCls="bg-purple-950/20"
                          defaultOpen={false}
                        >
                          <ul className="space-y-1">
                            {snap.alternative_considerations.map((item, i) => (
                              <li key={i} className="flex gap-2 text-xs text-white/80 leading-snug">
                                <span className="text-purple-400 flex-shrink-0">›</span>
                                <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                                  {preprocessWikiLinks(item)}
                                </ReactMarkdown>
                              </li>
                            ))}
                          </ul>
                        </Accordion>
                      )}

                      {/* Fallback: old runs using immediate_actions without clinical_direction */}
                      {!snap.clinical_direction?.length && snap.immediate_actions?.length > 0 && (
                        <Accordion
                          label="Immediate Actions"
                          labelCls="text-red-400"
                          borderCls="border-red-800/50"
                          bgCls="bg-red-950/20"
                          defaultOpen={true}
                        >
                          <ul className="space-y-1">
                            {snap.immediate_actions.map((item, i) => (
                              <li key={i} className="flex gap-2 text-xs text-white/80 leading-snug">
                                <span className="text-red-400 flex-shrink-0">›</span>
                                <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                                  {preprocessWikiLinks(item)}
                                </ReactMarkdown>
                              </li>
                            ))}
                          </ul>
                        </Accordion>
                      )}

                      {/* Fallback for very old runs with plain agent_answer */}
                      {!snap.clinical_direction?.length && !snap.immediate_actions?.length && snap.agent_answer && (
                        <div className="prose-sm text-white/80 text-sm leading-relaxed bg-ink-900 rounded-lg p-3 border border-border">
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                            {preprocessWikiLinks(snap.agent_answer)}
                          </ReactMarkdown>
                        </div>
                      )}
                    </div>
                  )}
                  {/* Pages consulted */}
                  {snap.pages_consulted?.length > 0 && (
                    <div className="mt-2">
                      <p className="text-[10px] uppercase tracking-widest text-muted mb-1">Wiki pages consulted</p>
                      <div className="flex flex-wrap gap-1">
                        {snap.pages_consulted.map((p, i) => (
                          <span key={i} className="inline-flex px-2 py-0.5 rounded bg-ink-800 border border-border text-[10px] font-mono text-white/50">
                            {p.split('/').pop()}
                          </span>
                        ))}
                      </div>
                    </div>
                  )}
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
  const [selectedModel, setSelectedModel] = useState('')
  const [selectedReasoningModel, setSelectedReasoningModel] = useState('')
  const [selectedSnapshot, setSelectedSnapshot] = useState('1')
  const [availableSnapshots, setAvailableSnapshots] = useState([])
  const [usePatientContext, setUsePatientContext] = useState(true)
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

  useEffect(() => {
    if (!selectedPatient) { setAvailableSnapshots([]); return }
    listPatientSnapshots(selectedPatient)
      .then(data => setAvailableSnapshots(data.snapshots || []))
      .catch(() => setAvailableSnapshots([]))
  }, [selectedPatient])

  const handleRun = async () => {
    if (!selectedPatient) return
    setRunning(true)
    setError(null)
    try {
      const snapNum = selectedSnapshot !== '' ? parseInt(selectedSnapshot, 10) : null
      const { job_id } = await runClinicalAssessment(selectedPatient, activeKB, selectedModel || null, snapNum, usePatientContext, selectedReasoningModel || null)
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
      <label className="block text-xs text-muted mb-0.5 font-mono">Grounding model</label>
      <select
        value={selectedModel}
        onChange={e => setSelectedModel(e.target.value)}
        disabled={running}
        className="w-full bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white font-mono focus:outline-none focus:border-accent mb-2"
      >
        {AVAILABLE_MODELS.map(m => (
          <option key={m.value} value={m.value}>{m.label}</option>
        ))}
      </select>
      <label className="block text-xs text-muted mb-0.5 font-mono">Reasoning model</label>
      <select
        value={selectedReasoningModel}
        onChange={e => setSelectedReasoningModel(e.target.value)}
        disabled={running}
        className="w-full bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white font-mono focus:outline-none focus:border-accent mb-2"
      >
        {REASONING_MODELS.map(m => (
          <option key={m.value} value={m.value}>{m.label}</option>
        ))}
      </select>
      <select
        value={selectedSnapshot}
        onChange={e => setSelectedSnapshot(e.target.value)}
        disabled={running}
        className="w-full bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white font-mono focus:outline-none focus:border-accent mb-2"
      >
        <option value="">All snapshots</option>
        {availableSnapshots.map(s => (
          <option key={s.num} value={s.num}>{s.label}</option>
        ))}
      </select>
      {/* Patient context toggle */}
      <button
        onClick={() => setUsePatientContext(v => !v)}
        disabled={running}
        className={`w-full flex items-center justify-between px-3 py-2 rounded border text-xs font-mono mb-2 transition-colors disabled:opacity-40 ${
          usePatientContext
            ? 'bg-purple-950/40 border-purple-700/60 text-purple-300'
            : 'bg-ink-800 border-border text-muted'
        }`}
      >
        <span>Patient context in search</span>
        <span className={`w-8 h-4 rounded-full relative transition-colors ${usePatientContext ? 'bg-purple-500' : 'bg-ink-600'}`}>
          <span className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all ${usePatientContext ? 'left-4' : 'left-0.5'}`} />
        </span>
      </button>

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
  const [comment, setComment] = useState('')
  const [commentSaving, setCommentSaving] = useState(false)

  useEffect(() => {
    if (detail?.snapshots) {
      const initial = {}
      detail.snapshots.forEach(s => { if (s.rating != null) initial[s.snapshot_num] = s.rating })
      setRatings(initial)
      setOpenSnap(detail.snapshots[0]?.snapshot_num ?? null)
    }
    setComment(detail?.comment ?? '')
  }, [detail])

  const handleSaveComment = async () => {
    if (!detail) return
    setCommentSaving(true)
    try {
      await saveRunComment(detail.patient_id, detail.run_id, comment, activeKB)
      setAssessments(prev => prev.map(a =>
        a.run_id === detail.run_id ? { ...a, comment } : a
      ))
    } catch (err) {
      console.error(err)
    } finally {
      setCommentSaving(false)
    }
  }

  const handleRated = (snapNum, rating) => {
    const newRatings = { ...ratings, [snapNum]: rating }
    setRatings(newRatings)
    const values = Object.values(newRatings)
    const avg = values.length > 0
      ? Math.round(values.reduce((a, b) => a + b, 0) / values.length * 10) / 10
      : null
    setAssessments(prev => prev.map(a =>
      a.run_id === detail?.run_id ? { ...a, avg_rating: avg } : a
    ))
  }

  const handleDelete = async (run) => {
    await deleteClinicalAssessment(run.patient_id, run.run_id, activeKB)
    setAssessments(prev => prev.filter(a => a.run_id !== run.run_id))
    if (selectedRun?.run_id === run.run_id) {
      setSelectedRun(null)
      setDetail(null)
    }
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
                onDelete={handleDelete}
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
              <div className="flex items-center gap-3 text-xs text-muted flex-wrap">
                <span className="font-mono text-white/40">{detail.run_id}</span>
                <span className="text-border">·</span>
                <span>{detail.snapshots?.length ?? 0} snapshots</span>
                {detail.model && (
                  <>
                    <span className="text-border">·</span>
                    <span className="font-mono text-purple-400/80 bg-purple-950/30 border border-purple-800/40 px-1.5 py-0.5 rounded text-[10px]">
                      {detail.model}
                    </span>
                  </>
                )}
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

            {/* Comment */}
            <div className="mb-6">
              <p className="text-[10px] uppercase tracking-widest text-muted mb-1.5">Run Notes</p>
              <div className="relative">
                <textarea
                  value={comment}
                  onChange={e => setComment(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSaveComment() } }}
                  placeholder="Add notes about this run… (Enter to save, Shift+Enter for new line)"
                  rows={3}
                  className="w-full bg-ink-900 border border-border rounded-lg px-3 py-2 text-xs text-white/80 placeholder:text-muted/50 focus:outline-none focus:border-accent resize-none leading-relaxed"
                />
                {commentSaving && (
                  <span className="absolute bottom-2 right-2 text-[10px] text-muted/60">saving…</span>
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
