import { useState, useEffect } from 'react'
import {
  ExclamationTriangleIcon,
  ArrowPathIcon,
  CheckCircleIcon,
  ClockIcon,
  ArrowTopRightOnSquareIcon,
} from '@heroicons/react/24/outline'
import { getGapIntelligence, resolveAll, resolveBatchStatus } from '../api'
import { useAppState } from '../AppStateContext'

const TABS = [
  { id: 'persistent', label: 'Persistent KGs' },
  { id: 'open',       label: 'Open Gaps' },
  { id: 'scope',      label: 'Scope Contamination' },
]

const _SECTION_THRESHOLD = 2

function Badge({ children, cls }) {
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-mono border ${cls}`}>
      {children}
    </span>
  )
}

function SectionPill({ section, count }) {
  const isPersistent = count >= _SECTION_THRESHOLD
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] border ${
      isPersistent
        ? 'bg-red-500/10 border-red-500/40 text-red-400'
        : 'bg-ink-800 border-border text-muted'
    }`}>
      {section}
      <span className={`font-mono text-[9px] ${isPersistent ? 'text-red-400' : 'text-muted'}`}>
        ×{count}
      </span>
    </span>
  )
}

function PersistentGapCard({ gap }) {
  const maxOpens = gap.max_section_opens
  const urgency = maxOpens >= 5 ? 'border-red-600/50 bg-red-950/10'
                : maxOpens >= 3 ? 'border-amber-600/50 bg-amber-950/10'
                : 'border-yellow-700/40 bg-yellow-950/10'
  return (
    <div className={`rounded-lg border p-4 space-y-2 ${urgency}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-white">{gap.title}</p>
          <p className="text-[10px] text-muted font-mono mt-0.5">{gap.stem}</p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {gap.is_open
            ? <Badge cls="bg-amber-500/10 border-amber-500/40 text-amber-400">open</Badge>
            : <Badge cls="bg-emerald-500/10 border-emerald-500/40 text-emerald-400">resolved</Badge>}
          <Badge cls="bg-red-500/10 border-red-500/40 text-red-400">
            {maxOpens}× reopened
          </Badge>
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {Object.entries(gap.section_times_opened).map(([s, cnt]) => (
          <SectionPill key={s} section={s} count={cnt} />
        ))}
      </div>
      <p className="text-[10px] text-muted">
        {maxOpens >= _SECTION_THRESHOLD
          ? '⚡ Will use LLM fallback directly on next resolve (skips PubMed)'
          : `${_SECTION_THRESHOLD - maxOpens} more filing(s) until LLM escalation`}
      </p>
    </div>
  )
}

function OpenGapCard({ gap }) {
  const maxOpens = gap.max_section_opens || gap.times_opened
  return (
    <div className="rounded-lg border border-border bg-ink-900 p-4 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-white">{gap.title}</p>
          <p className="text-[10px] text-muted font-mono mt-0.5">{gap.referenced_page}</p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <Badge cls={gap.placement === 'confirmed'
            ? 'bg-emerald-500/10 border-emerald-500/40 text-emerald-400'
            : 'bg-amber-500/10 border-amber-500/40 text-amber-400'}>
            {gap.placement}
          </Badge>
          {maxOpens >= _SECTION_THRESHOLD && (
            <Badge cls="bg-red-500/10 border-red-500/40 text-red-400">
              {maxOpens}× opened
            </Badge>
          )}
        </div>
      </div>

      {gap.missing_sections.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {gap.missing_sections.map(s => (
            <SectionPill
              key={s}
              section={s}
              count={(gap.section_times_opened || {})[s] || 0}
            />
          ))}
        </div>
      )}

      {gap.missing_values.length > 0 && (
        <div className="space-y-0.5">
          {gap.missing_values.slice(0, 3).map((v, i) => (
            <p key={i} className="text-[10px] text-white/60 truncate">· {v}</p>
          ))}
          {gap.missing_values.length > 3 && (
            <p className="text-[10px] text-muted">+{gap.missing_values.length - 3} more</p>
          )}
        </div>
      )}

      {gap.resolution_question && (
        <p className="text-[10px] text-accent/70 italic truncate">{gap.resolution_question}</p>
      )}
    </div>
  )
}

function ScopeCard({ item }) {
  return (
    <div className="rounded-lg border border-orange-700/40 bg-orange-950/10 p-4 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-white">{item.title}</p>
          <p className="text-[10px] text-muted font-mono mt-0.5">{item.path}</p>
        </div>
        <Badge cls="bg-orange-500/10 border-orange-500/40 text-orange-400">
          {item.violations.length} violation{item.violations.length !== 1 ? 's' : ''}
        </Badge>
      </div>
      <div className="space-y-1.5">
        {item.violations.map((v, i) => (
          <div key={i} className="rounded bg-ink-800 px-3 py-1.5 space-y-0.5">
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-orange-400 font-medium">§ {v.section}</span>
              {v.belongs_on && (
                <span className="text-[10px] text-muted">→ belongs on <span className="text-white/70">{v.belongs_on}</span></span>
              )}
            </div>
            {v.excerpt && (
              <p className="text-[10px] text-white/50 italic truncate">"{v.excerpt}"</p>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function GapIntelligence() {
  const { activeKB } = useAppState()
  const [tab, setTab] = useState('persistent')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [resolving, setResolving] = useState(false)
  const [batchId, setBatchId] = useState(null)
  const [batchStatus, setBatchStatus] = useState(null)

  async function load() {
    setLoading(true)
    try {
      const d = await getGapIntelligence(activeKB)
      setData(d)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [activeKB])

  // Poll batch status
  useEffect(() => {
    if (!batchId) return
    const id = setInterval(async () => {
      try {
        const s = await resolveBatchStatus(batchId)
        setBatchStatus(s)
        if (s.status === 'done') {
          clearInterval(id)
          setResolving(false)
          setBatchId(null)
          setBatchStatus(null)
          load()
        }
      } catch { clearInterval(id) }
    }, 3000)
    return () => clearInterval(id)
  }, [batchId])

  async function handleResolveAll() {
    setResolving(true)
    try {
      const r = await resolveAll(activeKB, 3)
      setBatchId(r.batch_id)
    } catch (e) {
      alert(`Resolve failed: ${e.message}`)
      setResolving(false)
    }
  }

  const openCount       = data?.open_gaps?.length ?? 0
  const persistentCount = data?.persistent_gaps?.length ?? 0
  const scopeCount      = data?.scope_contamination?.length ?? 0

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-border flex-shrink-0">
        <div>
          <h1 className="text-lg font-semibold text-white">Gap Intelligence</h1>
          <p className="text-xs text-muted mt-0.5">
            {openCount} open · {persistentCount} persistent · {scopeCount} scope issues
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={load}
            disabled={loading}
            className="p-2 rounded-md text-muted hover:text-white hover:bg-ink-700 transition-colors"
          >
            <ArrowPathIcon className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
          <button
            onClick={handleResolveAll}
            disabled={resolving || openCount === 0}
            className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-accent text-ink-950 text-xs font-medium hover:bg-accent/90 disabled:opacity-50 transition-colors"
          >
            {resolving ? (
              <>
                <ArrowPathIcon className="w-3.5 h-3.5 animate-spin" />
                {batchStatus
                  ? `${batchStatus.completed_gaps}/${batchStatus.total_gaps}`
                  : 'Starting…'}
              </>
            ) : (
              <>
                <CheckCircleIcon className="w-3.5 h-3.5" />
                Resolve All
              </>
            )}
          </button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex border-b border-border flex-shrink-0 px-6">
        {TABS.map(t => {
          const count = t.id === 'persistent' ? persistentCount
                      : t.id === 'open'       ? openCount
                      : scopeCount
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`relative px-4 py-3 text-xs font-medium transition-colors flex items-center gap-2 ${
                tab === t.id
                  ? 'text-accent border-b-2 border-accent -mb-px'
                  : 'text-muted hover:text-white'
              }`}
            >
              {t.label}
              {count > 0 && (
                <span className={`text-[9px] font-mono px-1 py-0.5 rounded ${
                  tab === t.id ? 'bg-accent/20 text-accent' : 'bg-ink-700 text-muted'
                }`}>{count}</span>
              )}
            </button>
          )
        })}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto p-6">
        {loading ? (
          <div className="flex items-center justify-center h-32 text-muted text-sm">
            <ArrowPathIcon className="w-4 h-4 animate-spin mr-2" /> Loading…
          </div>
        ) : (
          <>
            {tab === 'persistent' && (
              <div className="space-y-3 max-w-3xl">
                {data?.persistent_gaps?.length === 0 ? (
                  <div className="text-center py-16 text-muted text-sm">
                    <CheckCircleIcon className="w-8 h-8 mx-auto mb-3 opacity-40" />
                    No persistent gaps yet — all gaps have been resolved within 2 attempts.
                  </div>
                ) : (
                  <>
                    <p className="text-xs text-muted mb-4">
                      Gaps opened ≥{_SECTION_THRESHOLD} times across learning cycles. These will bypass PubMed and use LLM fallback directly on next resolve.
                    </p>
                    {data.persistent_gaps.map(g => (
                      <PersistentGapCard key={g.stem} gap={g} />
                    ))}
                  </>
                )}
              </div>
            )}

            {tab === 'open' && (
              <div className="space-y-3 max-w-3xl">
                {data?.open_gaps?.length === 0 ? (
                  <div className="text-center py-16 text-muted text-sm">
                    <CheckCircleIcon className="w-8 h-8 mx-auto mb-3 opacity-40" />
                    No open knowledge gaps.
                  </div>
                ) : (
                  data.open_gaps.map(g => <OpenGapCard key={g.stem} gap={g} />)
                )}
              </div>
            )}

            {tab === 'scope' && (
              <div className="space-y-3 max-w-3xl">
                {data?.scope_contamination?.length === 0 ? (
                  <div className="text-center py-16 text-muted text-sm">
                    <CheckCircleIcon className="w-8 h-8 mx-auto mb-3 opacity-40" />
                    No scope contamination detected.
                  </div>
                ) : (
                  <>
                    <p className="text-xs text-muted mb-4">
                      Wiki pages containing content that belongs on a different page.
                      Run the defrag pipeline to automatically relocate these sections.
                    </p>
                    {data.scope_contamination.map(item => (
                      <ScopeCard key={item.path} item={item} />
                    ))}
                  </>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
