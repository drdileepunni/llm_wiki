import { useEffect, useState, useCallback } from 'react'
import { ArrowPathIcon } from '@heroicons/react/24/outline'
import { getWikiActivity } from '../api'
import { useAppState } from '../AppStateContext'

const OP_STYLE = {
  ingest:      'bg-accent/20 text-accent',
  gap_resolve: 'bg-success/20 text-success',
}

function Chip({ children, variant = 'default' }) {
  const cls = {
    default:  'bg-ink-800 text-white/70',
    warning:  'bg-warning/10 text-warning',
    success:  'bg-success/10 text-success',
    accent:   'bg-accent/10 text-accent',
  }[variant]
  return (
    <span className={`px-1.5 py-0.5 rounded text-xs font-mono ${cls}`}>
      {children}
    </span>
  )
}

function EventCard({ event }) {
  const [open, setOpen] = useState(false)
  const hasDetails = (
    event.files_written?.length > 0 ||
    event.gaps_opened?.length > 0 ||
    event.gaps_closed?.length > 0 ||
    event.sections_filled?.length > 0
  )

  return (
    <div
      className={`p-4 bg-surface border border-border rounded-xl transition-colors ${hasDetails ? 'cursor-pointer hover:border-accent/30' : ''}`}
      onClick={() => hasDetails && setOpen(o => !o)}
    >
      {/* Summary row */}
      <div className="flex items-center gap-3 min-w-0">
        <span className={`shrink-0 px-2 py-0.5 rounded text-xs font-mono ${OP_STYLE[event.operation] || 'bg-ink-700 text-muted'}`}>
          {event.operation}
        </span>
        <span className="text-white text-sm font-medium truncate flex-1 min-w-0">
          {event.source}
        </span>
        <span className="shrink-0 text-warning font-mono text-xs">
          ${(event.cost_usd ?? 0).toFixed(4)}
        </span>
        <span className="shrink-0 text-muted font-mono text-xs whitespace-nowrap">
          {new Date(event.timestamp).toLocaleString()}
        </span>
        {hasDetails && (
          <span className="shrink-0 text-muted text-xs">{open ? '▲' : '▼'}</span>
        )}
      </div>

      {/* Detail rows */}
      {open && (
        <div className="mt-3 space-y-2 pl-1">
          {event.files_written?.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs font-mono text-muted w-20 shrink-0">written</span>
              {event.files_written.map(f => (
                <Chip key={f}>{f.replace('wiki/', '')}</Chip>
              ))}
            </div>
          )}
          {event.gaps_opened?.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs font-mono text-muted w-20 shrink-0">gaps ＋</span>
              {event.gaps_opened.map(g => (
                <Chip key={g} variant="warning">{g.replace('wiki/gaps/', '')}</Chip>
              ))}
            </div>
          )}
          {event.gaps_closed?.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs font-mono text-muted w-20 shrink-0">gaps −</span>
              {event.gaps_closed.map(g => (
                <Chip key={g} variant="success">{g.replace('wiki/gaps/', '').replace('wiki/', '')}</Chip>
              ))}
            </div>
          )}
          {event.sections_filled?.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs font-mono text-muted w-20 shrink-0">filled</span>
              {event.sections_filled.map(s => (
                <Chip key={s} variant="accent">{s}</Chip>
              ))}
            </div>
          )}
          <div className="flex gap-4 text-xs font-mono text-muted pt-1">
            <span>{(event.tokens_in ?? 0).toLocaleString()} in</span>
            <span>{(event.tokens_out ?? 0).toLocaleString()} out</span>
          </div>
        </div>
      )}
    </div>
  )
}

export default function Activity() {
  const { activeKB } = useAppState()
  const [events, setEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [opFilter, setOpFilter] = useState('all')
  const [search, setSearch] = useState('')

  const load = useCallback(() => {
    setLoading(true)
    getWikiActivity(activeKB)
      .then(d => setEvents(d.events || []))
      .catch(() => setEvents([]))
      .finally(() => setLoading(false))
  }, [activeKB])

  useEffect(() => { load() }, [load])

  const filtered = events
    .filter(e => opFilter === 'all' || e.operation === opFilter)
    .filter(e => {
      if (!search) return true
      const q = search.toLowerCase()
      return (
        e.source?.toLowerCase().includes(q) ||
        e.files_written?.some(f => f.toLowerCase().includes(q)) ||
        e.gaps_opened?.some(g => g.toLowerCase().includes(q)) ||
        e.gaps_closed?.some(g => g.toLowerCase().includes(q))
      )
    })

  const totalCost = filtered.reduce((s, e) => s + (e.cost_usd ?? 0), 0)

  return (
    <div className="h-full overflow-y-auto px-8 py-8 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-display text-2xl font-semibold text-white">Activity</h1>
          <p className="text-sm text-muted mt-0.5">Wiki change history for <span className="text-accent font-mono">{activeKB}</span></p>
        </div>
        <button
          onClick={load}
          className="flex items-center gap-1.5 text-xs font-mono text-muted hover:text-white transition-colors"
        >
          <ArrowPathIcon className="w-3.5 h-3.5" />
          refresh
        </button>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded-lg border border-border overflow-hidden">
          {['all', 'ingest', 'gap_resolve'].map(op => (
            <button
              key={op}
              onClick={() => setOpFilter(op)}
              className={`px-3 py-1.5 text-xs font-mono transition-colors ${
                opFilter === op
                  ? 'bg-accent/20 text-accent'
                  : 'text-muted hover:text-white bg-ink-900'
              }`}
            >
              {op}
            </button>
          ))}
        </div>
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="filter by source or page…"
          className="flex-1 min-w-48 bg-ink-900 border border-border rounded-lg px-3 py-1.5 text-xs font-mono text-white placeholder:text-muted focus:outline-none focus:border-accent"
        />
        <span className="text-xs font-mono text-muted">
          {filtered.length} events · <span className="text-warning">${totalCost.toFixed(4)}</span>
        </span>
      </div>

      {/* Feed */}
      {loading ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : filtered.length === 0 ? (
        <div className="py-16 text-center">
          <p className="text-muted text-sm">No activity yet.</p>
          <p className="text-muted text-xs mt-1">Run an ingest or resolve a gap to start tracking changes.</p>
        </div>
      ) : (
        <div className="space-y-2">
          {filtered.map((e, i) => <EventCard key={i} event={e} />)}
        </div>
      )}
    </div>
  )
}
