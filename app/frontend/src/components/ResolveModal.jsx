import { useState, useEffect } from 'react'
import { XMarkIcon, MagnifyingGlassIcon } from '@heroicons/react/24/outline'
import { resolveSearch, resolveIngest } from '../api'
import { useAppState } from '../AppStateContext'

function Spinner({ className = 'w-4 h-4' }) {
  return (
    <svg className={`animate-spin ${className}`} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

export default function ResolveModal({ gap, onClose, onJobsStarted }) {
  const { activeKB } = useAppState()
  const [phase, setPhase]       = useState('searching')  // searching | results | error
  const [articles, setArticles] = useState([])
  const [selected, setSelected] = useState(new Set())
  const [error, setError]       = useState(null)
  const [ingesting, setIngesting] = useState(false)

  useEffect(() => {
    resolveSearch(gap.title, gap.missing_sections, activeKB)
      .then(data => {
        setArticles(data.articles || [])
        setPhase('results')
      })
      .catch(e => {
        setError(e.message)
        setPhase('error')
      })
  }, [])

  const toggle = (pmcId) => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(pmcId) ? next.delete(pmcId) : next.add(pmcId)
      return next
    })
  }

  const handleIngest = async () => {
    setIngesting(true)
    const toIngest = articles.filter(a => selected.has(a.pmc_id))
    const jobs = []
    for (const article of toIngest) {
      try {
        const { job_id } = await resolveIngest(article.pmc_id, article.title, article.citation, activeKB)
        jobs.push({ job_id, title: article.title, pmc_id: article.pmc_id, status: 'running' })
      } catch {
        // collect remaining jobs even if one fails
      }
    }
    onJobsStarted(jobs)
    onClose()
  }

  return (
    <div
      className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        className="bg-ink-900 border border-border rounded-2xl w-full max-w-2xl max-h-[85vh] flex flex-col shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        {/* ── Header ── */}
        <div className="p-5 border-b border-border flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs font-mono text-muted uppercase tracking-wider mb-0.5">Resolve knowledge gap</p>
            <h2 className="font-display text-lg font-semibold text-amber-300 truncate">{gap.title}</h2>
            <div className="flex flex-wrap gap-1 mt-2">
              {gap.missing_sections.map((s, i) => (
                <span
                  key={i}
                  className="px-1.5 py-0.5 text-xs bg-amber-900/30 border border-amber-800/30 rounded text-amber-400/80 font-mono"
                >
                  {s}
                </span>
              ))}
            </div>
          </div>
          <button onClick={onClose} className="flex-shrink-0 text-muted hover:text-white transition-colors mt-1">
            <XMarkIcon className="w-5 h-5" />
          </button>
        </div>

        {/* ── Body ── */}
        <div className="flex-1 overflow-y-auto p-5">

          {phase === 'searching' && (
            <div className="flex flex-col items-center justify-center py-16 gap-3">
              <div className="flex items-center gap-3 text-accent">
                <Spinner className="w-5 h-5" />
                <span className="text-sm text-white">Searching PubMed via Google…</span>
              </div>
              <p className="text-xs text-muted/60">Claude is generating queries · This takes ~20–30 seconds</p>
            </div>
          )}

          {phase === 'error' && (
            <div className="p-4 bg-red-950/50 border border-red-800 rounded-lg text-sm text-red-400">
              {error}
            </div>
          )}

          {phase === 'results' && articles.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 gap-2 text-muted">
              <MagnifyingGlassIcon className="w-8 h-8 mb-1 opacity-50" />
              <p className="text-sm">No free full-text articles found</p>
              <p className="text-xs opacity-60">Try ingesting a specific URL manually</p>
            </div>
          )}

          {phase === 'results' && articles.length > 0 && (
            <div className="flex flex-col gap-3">
              <p className="text-xs text-muted">
                Found <span className="text-white">{articles.length}</span> relevant article{articles.length !== 1 ? 's' : ''} with free full text. Select to ingest.
              </p>
              {articles.map((article, i) => (
                <label
                  key={i}
                  className={`flex gap-3 p-4 rounded-xl border cursor-pointer transition-all ${
                    selected.has(article.pmc_id)
                      ? 'bg-accent/10 border-accent/40'
                      : 'bg-ink-800 border-border hover:border-border/60 hover:bg-ink-700'
                  }`}
                >
                  <input
                    type="checkbox"
                    className="mt-0.5 flex-shrink-0 accent-accent"
                    checked={selected.has(article.pmc_id)}
                    onChange={() => toggle(article.pmc_id)}
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-white leading-snug mb-1">
                      {article.title}
                    </p>
                    <p className="text-xs text-muted font-mono mb-1.5">
                      {[article.journal, article.pub_date, `PMID:${article.pmid}`].filter(Boolean).join(' · ')}
                    </p>
                    <p className="text-xs text-green-400/80 mb-2">
                      ✓ {article.relevance_reason}
                    </p>
                    {article.abstract && (
                      <p className="text-xs text-muted/60 line-clamp-2">
                        {article.abstract.slice(0, 280)}…
                      </p>
                    )}
                  </div>
                </label>
              ))}
            </div>
          )}
        </div>

        {/* ── Footer ── */}
        {phase === 'results' && (
          <div className="p-5 border-t border-border flex items-center justify-between">
            <button
              onClick={onClose}
              className="px-4 py-2 text-sm text-muted hover:text-white transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleIngest}
              disabled={selected.size === 0 || ingesting}
              className="px-5 py-2 bg-accent hover:bg-accent-dim disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition-colors flex items-center gap-2"
            >
              {ingesting && <Spinner className="w-3.5 h-3.5" />}
              {ingesting ? 'Starting…' : `Ingest Selected (${selected.size})`}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
