import { useEffect, useState, useCallback, useRef } from 'react'
import { ArrowPathIcon, DocumentTextIcon, XMarkIcon } from '@heroicons/react/24/outline'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  getWikiActivity, getWikiFile,
  getWikiGaps, resolveAll, resolveBatchStatus, deleteGap, updateGap, createGap, resolveJobStatus,
  getWikiContamination, runDefrag, runMigrateScope, runScanContamination,
  markFalsePositive, runReconcileGaps,
} from '../api'
import { useAppState } from '../AppStateContext'
import ResolveModal from '../components/ResolveModal'

function stripFrontmatter(text) {
  return text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '')
}

// Parse section_quality scores from raw frontmatter.
// Returns { "section name (lowercase)": { score, flags } }
function parseSectionQuality(rawContent) {
  const fmMatch = rawContent.match(/^---\r?\n([\s\S]*?)\r?\n---/)
  if (!fmMatch) return {}
  const fm = fmMatch[1]
  const quality = {}
  // Match YAML entries like:  "Section Name":\n    score: 3\n    flags: [...]
  const re = /"([^"]+)":\s*\n\s+score:\s*(\d+)(?:\s*\n\s+flags:\s*\[([^\]]*)\])?/g
  let m
  while ((m = re.exec(fm)) !== null) {
    const flags = m[3] ? m[3].split(',').map(s => s.trim()).filter(Boolean) : []
    quality[m[1].toLowerCase()] = { score: parseInt(m[2], 10), flags }
  }
  return quality
}

const QUALITY_MAX = 4

function QualityBadge({ score, flags }) {
  const pct = score / QUALITY_MAX
  const [bg, text, border] =
    pct >= 1.0 ? ['bg-green-900/40',  'text-green-300',  'border-green-700/50'] :
    pct >= 0.6 ? ['bg-accent/10',     'text-accent/80',  'border-accent/30']    :
    pct >= 0.4 ? ['bg-amber-900/30',  'text-amber-400',  'border-amber-700/40'] :
                 ['bg-red-900/30',    'text-red-400',    'border-red-700/40']
  const hasFlags = flags?.length > 0
  return (
    <span
      title={hasFlags ? flags.join(', ') : `Quality: ${score}/${QUALITY_MAX}`}
      className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-mono border ml-2 ${bg} ${text} ${border}`}
    >
      {score}/{QUALITY_MAX}
      {hasFlags && <span className="opacity-60 ml-0.5">⚠</span>}
    </span>
  )
}

// Convert [[Wiki Link]] → a markdown link with a special wiki:// scheme
// so the custom `a` renderer can style it as a wiki badge.
function preprocessWikiLinks(text) {
  return text.replace(/\[\[([^\]]+)\]\]/g, (_, title) => {
    const slug = title.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9-]/g, '')
    return `[${title}](wiki://${slug})`
  })
}

function makeMdComponents(sectionQuality = {}) {
  return {
    p({ children })      { return <p className="mb-3 last:mb-0 leading-relaxed">{children}</p> },
    ul({ children })     { return <ul className="list-disc list-outside ml-5 mb-3 space-y-1">{children}</ul> },
    ol({ children })     { return <ol className="list-decimal list-outside ml-5 mb-3 space-y-1">{children}</ol> },
    li({ children })     { return <li className="leading-relaxed">{children}</li> },
    strong({ children }) { return <strong className="font-semibold text-white">{children}</strong> },
    em({ children })     { return <em className="italic text-white/75">{children}</em> },
    h1({ children })     { return <h1 className="text-xl font-bold text-white mt-6 mb-3 pb-1 border-b border-border">{children}</h1> },
    h2({ children }) {
      const text = String(children)
      const q = sectionQuality[text.toLowerCase().trim()]
      return (
        <h2 className="flex items-center text-base font-bold text-white mt-4 mb-2">
          {text}
          {q && <QualityBadge score={q.score} flags={q.flags} />}
        </h2>
      )
    },
    h3({ children })     { return <h3 className="text-sm font-semibold text-white/90 mt-3 mb-1">{children}</h3> },
    code({ inline, children }) {
      return inline
        ? <code className="px-1 py-0.5 bg-ink-900 rounded text-xs font-mono text-accent/80">{children}</code>
        : <pre className="p-3 my-2 bg-ink-900 rounded-lg text-xs font-mono text-white/80 overflow-x-auto"><code>{children}</code></pre>
    },
    blockquote({ children }) {
      return <blockquote className="border-l-2 border-accent/40 pl-3 italic text-white/60 mb-3">{children}</blockquote>
    },
    table({ children })  { return <div className="overflow-x-auto mb-3"><table className="text-xs border-collapse w-full">{children}</table></div> },
    thead({ children })  { return <thead className="bg-ink-800">{children}</thead> },
    th({ children })     { return <th className="border border-border px-3 py-1.5 text-left font-semibold text-white">{children}</th> },
    td({ children })     { return <td className="border border-border px-3 py-1.5 text-white/80">{children}</td> },
    hr()                 { return <hr className="border-border my-4" /> },
    a({ href, children }) {
      if (href?.startsWith('wiki://')) {
        return (
          <span className="inline-flex items-center gap-0.5 px-1.5 py-0.5 bg-accent/10 border border-accent/20 rounded text-accent/90 text-xs font-medium">
            <span className="opacity-50 text-[9px]">⟨</span>{children}<span className="opacity-50 text-[9px]">⟩</span>
          </span>
        )
      }
      return <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent underline hover:text-accent/70">{children}</a>
    },
  }
}
// Default instance with no quality data (used when quality isn't available)
const MD_COMPONENTS = makeMdComponents()

const OP_STYLE = {
  ingest:      'bg-accent/20 text-accent',
  gap_resolve: 'bg-success/20 text-success',
  consolidate: 'bg-purple-500/20 text-purple-400',
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

function EventCard({ event, selected, onSelect }) {
  const hasFiles = event.files_written?.length > 0

  return (
    <div
      className={`p-3 border rounded-xl transition-colors cursor-pointer ${
        selected
          ? 'bg-accent/10 border-accent/50'
          : 'bg-surface border-border hover:border-accent/30'
      }`}
      onClick={() => onSelect(event)}
    >
      {/* Summary row */}
      <div className="flex items-center gap-2 min-w-0">
        <span className={`shrink-0 px-2 py-0.5 rounded text-xs font-mono ${OP_STYLE[event.operation] || 'bg-ink-700 text-muted'}`}>
          {event.operation}
        </span>
        <span className="text-white text-xs font-medium truncate flex-1 min-w-0">
          {event.source}
        </span>
        {hasFiles && <DocumentTextIcon className="w-3.5 h-3.5 shrink-0 text-muted" />}
      </div>

      {/* Meta row */}
      <div className="flex items-center gap-3 mt-1.5 pl-0.5">
        <span className="text-warning font-mono text-xs">${(event.cost_usd ?? 0).toFixed(4)}</span>
        <span className="text-muted font-mono text-xs">{new Date(event.timestamp).toLocaleString()}</span>
      </div>

      {/* Files written */}
      {event.files_written?.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {event.files_written.map(f => (
            <Chip key={f}>{f.replace('wiki/', '')}</Chip>
          ))}
        </div>
      )}
    </div>
  )
}

// ── Gaps panel components ─────────────────────────────────────────────────────

function JobCard({ job, onUpdate }) {
  useEffect(() => {
    if (job.status !== 'running') return
    const iv = setInterval(async () => {
      try {
        const data = await resolveJobStatus(job.job_id)
        if (data.status !== 'running') { onUpdate(job.job_id, data); clearInterval(iv) }
      } catch { clearInterval(iv) }
    }, 3000)
    return () => clearInterval(iv)
  }, [job.status])

  const colors = {
    running: 'bg-blue-950/20 border-blue-800/30 text-blue-300',
    done:    'bg-green-950/20 border-green-800/30 text-green-300',
    error:   'bg-red-950/20 border-red-800/30 text-red-300',
  }
  return (
    <div className={`p-3 rounded-lg border text-xs ${colors[job.status] || colors.error}`}>
      <div className="flex items-center gap-2 mb-0.5">
        {job.status === 'running' && (
          <svg className="animate-spin w-3 h-3 flex-shrink-0 text-blue-400" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
        )}
        {job.status === 'done'  && <span className="text-green-400 flex-shrink-0">✓</span>}
        {job.status === 'error' && <span className="text-red-400 flex-shrink-0">✗</span>}
        <p className="font-medium truncate">{job.title}</p>
      </div>
      {job.status === 'running' && <p className="text-muted ml-5">Fetching article + running ingest pipeline…</p>}
      {job.status === 'done' && job.result && (
        <p className="text-muted ml-5">
          {job.result.files_written?.length || 0} files written
          {job.result.cost_usd != null ? ` · $${job.result.cost_usd.toFixed(4)}` : ''}
        </p>
      )}
      {job.status === 'error' && <p className="text-red-400/70 ml-5 truncate">{job.error}</p>}
    </div>
  )
}

function GapCard({ gap, activeKB, onResolve, onDelete, deleting, onUpdated, isExpanded, onToggle }) {
  const stem = gap.file?.replace('wiki/gaps/', '').replace('.md', '') || gap.title
  const [editingTitle, setEditingTitle] = useState(false)
  const [titleVal, setTitleVal]         = useState(gap.title)
  const [sections, setSections]         = useState(gap.missing_sections || [])
  const [editingIdx, setEditingIdx]     = useState(null)
  const [editingVal, setEditingVal]     = useState('')
  const [saving, setSaving]             = useState(false)
  const [pageContent, setPageContent]   = useState(null)
  const [pageQuality, setPageQuality]   = useState({})
  const [pageLoading, setPageLoading]   = useState(false)

  // Load page content when expanded
  useEffect(() => {
    if (!isExpanded || pageContent !== null) return
    if (!gap.referenced_page) { setPageContent(''); return }
    setPageLoading(true)
    const filePath = gap.referenced_page.startsWith('wiki/') ? gap.referenced_page : `wiki/${gap.referenced_page}`
    getWikiFile(filePath, activeKB)
      .then(d => {
        const raw = d.content ?? ''
        setPageQuality(parseSectionQuality(raw))
        setPageContent(raw.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, ''))
      })
      .catch(() => setPageContent(''))
      .finally(() => setPageLoading(false))
  }, [isExpanded, gap.referenced_page, activeKB, pageContent])

  // Custom heading renderer: highlight missing sections in amber, show quality badges
  const missingSet = new Set((gap.missing_sections || []).map(s => s.toLowerCase().trim()))
  const mdComponents = {
    h2: ({ children }) => {
      const text = String(children)
      const key  = text.toLowerCase().trim()
      const isMissing = missingSet.has(key)
      const q = pageQuality[key]
      return isMissing ? (
        <div className="flex items-center gap-2 mt-4 mb-1">
          <h2 className="text-sm font-semibold text-amber-400/60 line-through">{text}</h2>
          <span className="text-[9px] px-1.5 py-0.5 rounded bg-amber-950 border border-amber-700/50 text-amber-500 font-mono">missing</span>
          {q && <QualityBadge score={q.score} flags={q.flags} />}
        </div>
      ) : (
        <div className="flex items-center gap-1 mt-4 mb-1 border-b border-border pb-1">
          <h2 className="text-sm font-semibold text-white/80">{text}</h2>
          {q && <QualityBadge score={q.score} flags={q.flags} />}
        </div>
      )
    },
    h3: ({ children }) => <h3 className="text-xs font-semibold text-white/70 mt-3 mb-1">{children}</h3>,
    p:  ({ children }) => <p className="text-xs text-white/60 leading-relaxed mb-2">{children}</p>,
    li: ({ children }) => <li className="text-xs text-white/60 leading-relaxed ml-3 list-disc">{children}</li>,
    ul: ({ children }) => <ul className="mb-2 space-y-0.5">{children}</ul>,
    ol: ({ children }) => <ol className="mb-2 space-y-0.5 list-decimal ml-3">{children}</ol>,
    code: ({ children }) => <code className="text-[10px] font-mono bg-ink-800 px-1 rounded text-accent/80">{children}</code>,
    strong: ({ children }) => <strong className="text-white/80 font-semibold">{children}</strong>,
    a: ({ href, children }) => {
      if (href?.startsWith('wiki://')) {
        return (
          <span className="inline-flex items-center gap-0.5 px-1 py-0.5 bg-accent/10 border border-accent/20 rounded text-accent/80 text-[10px] font-medium">
            <span className="opacity-40 text-[8px]">⟨</span>{children}<span className="opacity-40 text-[8px]">⟩</span>
          </span>
        )
      }
      return <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent/70 underline">{children}</a>
    },
  }

  const save = async (newTitle, newSections) => {
    setSaving(true)
    try {
      await updateGap(stem, { title: newTitle, missing_sections: newSections }, activeKB)
      onUpdated({ title: newTitle, missing_sections: newSections })
    } catch (e) { console.error(e) }
    finally { setSaving(false) }
  }

  const commitTitle = () => {
    setEditingTitle(false)
    if (titleVal.trim() && titleVal !== gap.title) save(titleVal.trim(), sections)
    else setTitleVal(gap.title)
  }

  const commitChip = (idx) => {
    const val = editingVal.trim()
    setEditingIdx(null)
    if (!val) { removeChip(idx); return }
    const next = sections.map((s, i) => i === idx ? val : s)
    setSections(next); save(titleVal, next)
  }

  const removeChip = (idx) => {
    const next = sections.filter((_, i) => i !== idx)
    setSections(next); save(titleVal, next)
  }

  const isApproximate = gap.placement === 'approximate'
  const isPersistent  = gap.status === 'persistent'

  return (
    <div className={`border rounded-xl transition-colors ${
      isPersistent
        ? `bg-red-950/15 ${isExpanded ? 'border-red-600/50' : 'border-red-800/40'}`
        : `bg-amber-950/20 ${isExpanded ? 'border-amber-600/50' : isApproximate ? 'border-amber-800/30 border-dashed' : 'border-amber-800/30'}`
    }`}>
      {/* Card header — always visible */}
      <div className="p-4">
        <div className="flex items-start justify-between gap-3 mb-2">
          <div className="flex-1 min-w-0">
            {editingTitle ? (
              <input
                autoFocus value={titleVal}
                onChange={e => setTitleVal(e.target.value)}
                onBlur={commitTitle}
                onKeyDown={e => { if (e.key === 'Enter') commitTitle(); if (e.key === 'Escape') { setEditingTitle(false); setTitleVal(gap.title) } }}
                className="w-full bg-amber-950/40 border border-amber-700/50 rounded px-2 py-0.5 text-sm font-semibold text-amber-300 focus:outline-none focus:border-amber-500"
              />
            ) : (
              <div className="flex items-center gap-1.5 group">
                <button
                  onClick={onToggle}
                  className={`text-sm font-semibold text-left transition-colors ${isPersistent ? 'text-red-300 hover:text-red-200' : 'text-amber-300 hover:text-amber-200'}`}
                >
                  {titleVal}
                </button>
                <button onClick={() => setEditingTitle(true)} className="opacity-0 group-hover:opacity-100 text-amber-600 hover:text-amber-400 transition-all text-[10px] border border-amber-800/40 rounded px-1 py-0.5">edit</button>
              </div>
            )}
          </div>
          <div className="flex items-center gap-1.5 flex-shrink-0">
            <button
              onClick={onToggle}
              className="w-6 h-6 flex items-center justify-center rounded text-amber-600 hover:text-amber-300 transition-colors text-xs"
              title={isExpanded ? 'Collapse preview' : 'Preview page'}
            >
              {isExpanded ? '▲' : '▼'}
            </button>
            <button onClick={onResolve} className="px-2.5 py-1 text-xs bg-accent/20 hover:bg-accent/40 border border-accent/30 rounded-md text-accent transition-colors font-medium">Resolve</button>
            <button onClick={onDelete} disabled={deleting} className="w-6 h-6 flex items-center justify-center rounded text-muted hover:text-red-400 hover:bg-red-950/30 border border-transparent hover:border-red-800/40 transition-all disabled:opacity-40 text-sm leading-none" title="Delete gap">×</button>
          </div>
        </div>
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <p className="text-xs text-muted font-mono">{gap.referenced_page}</p>
          {isApproximate && (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[9px] font-mono rounded border border-amber-700/40 bg-amber-950/50 text-amber-600/80"
              title="Section placement is approximate — the knowledge gap is real but may be filed under the wrong section. A defrag scan will correct the placement."
            >
              ~ approx. placement
            </span>
          )}
          {gap.status === 'persistent' && (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[9px] font-mono rounded border border-red-700/60 bg-red-950/50 text-red-400 font-semibold"
              title={`This gap has been opened ${gap.times_opened ?? gap.gap_opens ?? '?'} times — may need manual intervention`}
            >
              ⚠ persistent
            </span>
          )}
          {(gap.times_opened != null && gap.times_opened > 1) && (
            <span
              className={`inline-flex items-center px-1.5 py-0.5 text-[9px] font-mono rounded border ${
                gap.status === 'persistent'
                  ? 'border-red-800/40 bg-red-950/30 text-red-500/80'
                  : 'border-amber-800/40 bg-amber-950/30 text-amber-600/70'
              }`}
              title="Times this gap has been registered"
            >
              opened {gap.times_opened}×
            </span>
          )}
          {(gap.cds_query_count != null && gap.cds_query_count > 0) && (
            <span
              className="inline-flex items-center px-1.5 py-0.5 text-[9px] font-mono rounded border border-blue-800/40 bg-blue-950/30 text-blue-400/70"
              title="Times this page was retrieved during CDS queries — higher means clinicians are asking about it"
            >
              queried {gap.cds_query_count}× in CDS
            </span>
          )}
        </div>
        {/* Specific missing values — shown when available (more descriptive than section names) */}
        {(gap.missing_values?.length > 0) ? (
          <div className="space-y-1.5">
            <div className="flex flex-wrap gap-1 items-center">
              {gap.missing_values.map((v, j) => (
                <span key={j} className="inline-flex items-center px-2 py-0.5 text-xs bg-amber-900/25 border border-amber-700/30 rounded-full text-amber-300/90">
                  {v}
                </span>
              ))}
            </div>
            {/* Section badges as quiet secondary context */}
            <div className="flex flex-wrap gap-1 items-center">
              {sections.map((s, j) => (
                editingIdx === j ? (
                  <input key={j} autoFocus value={editingVal}
                    onChange={e => setEditingVal(e.target.value)}
                    onBlur={() => commitChip(j)}
                    onKeyDown={e => { if (e.key === 'Enter') commitChip(j); if (e.key === 'Escape') setEditingIdx(null) }}
                    className="px-1.5 py-0.5 text-xs bg-amber-900/50 border border-amber-600/50 rounded text-amber-300 font-mono focus:outline-none min-w-[120px]"
                  />
                ) : (
                  <span key={j} className="group inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] bg-amber-950/40 border border-amber-900/40 rounded text-amber-600/70 font-mono">
                    § {s}
                    <button onClick={() => { setEditingIdx(j); setEditingVal(s) }} className="opacity-0 group-hover:opacity-100 text-amber-600 hover:text-amber-300 transition-all leading-none" title="Edit">✎</button>
                    <button onClick={() => removeChip(j)} className="opacity-0 group-hover:opacity-100 text-amber-700 hover:text-red-400 transition-all leading-none" title="Remove">×</button>
                  </span>
                )
              ))}
              {saving && <span className="text-[10px] text-muted/60 font-mono ml-1">saving…</span>}
            </div>
          </div>
        ) : (
          /* Fallback: no specific values captured — show section chips as before */
          <div className="flex flex-wrap gap-1 items-center">
            {sections.map((s, j) => (
              editingIdx === j ? (
                <input key={j} autoFocus value={editingVal}
                  onChange={e => setEditingVal(e.target.value)}
                  onBlur={() => commitChip(j)}
                  onKeyDown={e => { if (e.key === 'Enter') commitChip(j); if (e.key === 'Escape') setEditingIdx(null) }}
                  className="px-1.5 py-0.5 text-xs bg-amber-900/50 border border-amber-600/50 rounded text-amber-300 font-mono focus:outline-none min-w-[120px]"
                />
              ) : (
                <span key={j} className="group inline-flex items-center gap-1 px-1.5 py-0.5 text-xs bg-amber-900/30 border border-amber-800/30 rounded text-amber-400/80 font-mono">
                  {s}
                  <button onClick={() => { setEditingIdx(j); setEditingVal(s) }} className="opacity-0 group-hover:opacity-100 text-amber-600 hover:text-amber-300 transition-all leading-none" title="Edit">✎</button>
                  <button onClick={() => removeChip(j)} className="opacity-0 group-hover:opacity-100 text-amber-700 hover:text-red-400 transition-all leading-none" title="Remove">×</button>
                </span>
              )
            ))}
            {saving && <span className="text-[10px] text-muted/60 font-mono ml-1">saving…</span>}
          </div>
        )}
      </div>

      {/* Inline page preview — only when expanded */}
      {isExpanded && (
        <div className={`border-t px-4 py-3 max-h-96 overflow-y-auto ${isPersistent ? 'border-red-800/30' : 'border-amber-800/30'}`}>
          {pageLoading ? (
            <p className="text-xs text-muted font-mono">Loading page…</p>
          ) : !pageContent ? (
            <p className="text-xs text-muted italic">No page content found.</p>
          ) : (
            <div className="prose-sm">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                {preprocessWikiLinks(pageContent ?? '')}
              </ReactMarkdown>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function NewGapForm({ activeKB, onCreated, onCancel }) {
  const [title, setTitle]         = useState('')
  const [chipInput, setChipInput] = useState('')
  const [sections, setSections]   = useState([])
  const [saving, setSaving]       = useState(false)
  const [error, setError]         = useState(null)

  const addChip = () => {
    const val = chipInput.trim()
    if (val && !sections.includes(val)) setSections(prev => [...prev, val])
    setChipInput('')
  }
  const handleChipKey = (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addChip() }
    if (e.key === 'Backspace' && !chipInput && sections.length) setSections(prev => prev.slice(0, -1))
  }
  const handleSave = async () => {
    const finalSections = chipInput.trim() ? [...sections, chipInput.trim()] : sections
    if (!title.trim() || !finalSections.length) { setError('Title and at least one section required.'); return }
    setSaving(true); setError(null)
    try {
      const result = await createGap(title.trim(), finalSections, activeKB)
      onCreated({ file: result.file, title: result.title, referenced_page: result.referenced_page, missing_sections: result.missing_sections })
    } catch (e) { setError(e.message) }
    finally { setSaving(false) }
  }

  return (
    <div className="p-4 bg-amber-950/10 border border-amber-700/40 rounded-xl space-y-3">
      <p className="text-xs font-mono text-amber-400 uppercase tracking-wider">New Knowledge Gap</p>
      <input
        placeholder="Topic title (e.g. Hypertriglyceridemia-induced pancreatitis)"
        value={title} onChange={e => setTitle(e.target.value)}
        className="w-full bg-ink-900 border border-border rounded-lg px-3 py-2 text-sm text-white placeholder:text-muted focus:outline-none focus:border-amber-600"
      />
      <div className="flex flex-wrap gap-1 items-center p-2 bg-ink-900 border border-border rounded-lg min-h-[38px]">
        {sections.map((s, i) => (
          <span key={i} className="inline-flex items-center gap-1 px-1.5 py-0.5 text-xs bg-amber-900/40 border border-amber-800/40 rounded text-amber-300 font-mono">
            {s}<button onClick={() => setSections(prev => prev.filter((_, j) => j !== i))} className="text-amber-700 hover:text-red-400 leading-none">×</button>
          </span>
        ))}
        <input
          value={chipInput} onChange={e => setChipInput(e.target.value)} onKeyDown={handleChipKey} onBlur={addChip}
          placeholder={sections.length ? 'Add section…' : 'Missing sections (Enter to add)'}
          className="flex-1 min-w-[140px] bg-transparent text-xs text-white placeholder:text-muted focus:outline-none font-mono"
        />
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}
      <div className="flex gap-2 justify-end">
        <button onClick={onCancel} className="px-3 py-1.5 text-xs text-muted hover:text-white transition-colors">Cancel</button>
        <button onClick={handleSave} disabled={saving} className="px-3 py-1.5 text-xs bg-amber-900/30 hover:bg-amber-900/50 border border-amber-700/40 rounded-md text-amber-300 transition-colors disabled:opacity-50">
          {saving ? 'Saving…' : 'Create Gap'}
        </button>
      </div>
    </div>
  )
}

function GapsPanel({ activeKB, onResolved }) {
  const [gaps, setGaps]               = useState([])
  const [loading, setLoading]         = useState(true)
  const [resolveGap, setResolveGap]   = useState(null)
  const [deletingGap, setDeletingGap] = useState(null)
  const [showNewGap, setShowNewGap]   = useState(false)
  const [jobs, setJobs]               = useState([])
  const [batchId, setBatchId]         = useState(null)
  const [batchInfo, setBatchInfo]     = useState(null)
  const [expandedGap, setExpandedGap] = useState(null)   // file key of the open card
  const addedJobIds                   = useRef(new Set())

  const refreshGaps = useCallback(() => {
    getWikiGaps(activeKB).then(d => setGaps(d.gaps || [])).catch(() => {})
  }, [activeKB])

  useEffect(() => {
    setLoading(true)
    getWikiGaps(activeKB)
      .then(d => setGaps(d.gaps || []))
      .catch(() => setGaps([]))
      .finally(() => setLoading(false))
  }, [activeKB])

  useEffect(() => {
    if (!batchId) return
    const iv = setInterval(async () => {
      try {
        const data = await resolveBatchStatus(batchId)
        setBatchInfo(data)
        const newJobs = (data.jobs || []).filter(j => !addedJobIds.current.has(j.job_id))
        if (newJobs.length > 0) {
          newJobs.forEach(j => addedJobIds.current.add(j.job_id))
          setJobs(prev => [...newJobs.map(j => ({ ...j, status: 'running' })), ...prev])
        }
        if (data.status === 'done') { clearInterval(iv); setBatchId(null); refreshGaps(); onResolved?.() }
      } catch { clearInterval(iv) }
    }, 3000)
    return () => clearInterval(iv)
  }, [batchId])

  const handleJobUpdate = (jobId, data) => {
    setJobs(prev => prev.map(j => j.job_id === jobId ? { ...j, ...data } : j))
    if (data.status === 'done') { refreshGaps(); onResolved?.() }
  }

  const handleDeleteGap = async (gap) => {
    const stem = gap.file?.replace('wiki/gaps/', '').replace('.md', '') || gap.title
    setDeletingGap(stem)
    try { await deleteGap(stem, activeKB); setGaps(prev => prev.filter(g => g.file !== gap.file)) }
    catch (err) { console.error(err) }
    finally { setDeletingGap(null) }
  }

  const handleResolveAll = async () => {
    try {
      addedJobIds.current = new Set()
      const data = await resolveAll(activeKB)
      setBatchId(data.batch_id)
      setBatchInfo({ total_gaps: gaps.length, completed_gaps: 0, status: 'running' })
    } catch (e) { console.error('resolve-all failed:', e) }
  }

  const [reconciling, setReconciling] = useState(false)
  const handleReconcile = async () => {
    setReconciling(true)
    try {
      const data = await runReconcileGaps(activeKB)
      // Poll after 3s — reconcile is fast (no LLM calls)
      setTimeout(() => { refreshGaps(); }, 3000)
    } catch (e) { console.error('reconcile failed:', e) }
    finally { setTimeout(() => setReconciling(false), 3000) }
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Header */}
      <div className="shrink-0 px-6 py-4 border-b border-border">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-xs font-mono text-amber-400 uppercase tracking-wider">
              Pending Knowledge Gaps{gaps.length > 0 ? ` — ${gaps.length} pages` : ''}
            </p>
            <p className="text-xs text-muted mt-0.5">These sections are missing from your wiki. Ingest a relevant source to fill them.</p>
          </div>
          <div className="flex items-center gap-1.5 shrink-0 ml-4">
            <button
              onClick={() => setShowNewGap(v => !v)}
              className="px-2.5 py-1 text-xs bg-amber-900/20 hover:bg-amber-900/40 border border-amber-700/30 rounded-md text-amber-400 transition-colors font-medium"
            >
              + New KG
            </button>
            <button
              onClick={handleReconcile}
              disabled={reconciling}
              title="Close gaps whose sections are already written in the wiki"
              className="px-2.5 py-1 text-xs bg-green-900/20 hover:bg-green-900/40 border border-green-700/30 rounded-md text-green-400 transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {reconciling ? 'Reconciling…' : '✓ Reconcile'}
            </button>
            {gaps.length > 0 && (
              <button
                onClick={handleResolveAll}
                disabled={!!batchId}
                className="px-2.5 py-1 text-xs bg-blue-900/20 hover:bg-blue-900/40 border border-blue-700/30 rounded-md text-blue-400 transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {batchId
                  ? `Resolving… ${batchInfo?.completed_gaps ?? 0}/${batchInfo?.total_gaps ?? gaps.length}`
                  : 'Resolve All'}
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3 min-h-0">
        {/* Active jobs */}
        {jobs.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs font-mono text-blue-400 uppercase tracking-wider">Active Resolutions — {jobs.length}</p>
            {jobs.map(job => <JobCard key={job.job_id} job={job} onUpdate={handleJobUpdate} />)}
          </div>
        )}

        {showNewGap && (
          <NewGapForm
            activeKB={activeKB}
            onCreated={(gap) => { setGaps(prev => [gap, ...prev]); setShowNewGap(false) }}
            onCancel={() => setShowNewGap(false)}
          />
        )}

        {loading ? (
          <p className="text-sm text-muted">Loading…</p>
        ) : gaps.length === 0 && !showNewGap ? (
          <div className="flex flex-col items-center justify-center py-16 text-center">
            <div className="w-12 h-12 rounded-full bg-ink-800 flex items-center justify-center mb-3">
              <DocumentTextIcon className="w-5 h-5 text-muted" />
            </div>
            <p className="text-sm text-muted">No pending gaps</p>
            <p className="text-xs text-muted/60 mt-1">Ingest a source to discover knowledge gaps automatically.</p>
          </div>
        ) : (
          gaps.map((gap, i) => (
            <GapCard
              key={gap.file || i}
              gap={gap}
              activeKB={activeKB}
              onResolve={() => setResolveGap(gap)}
              onDelete={() => handleDeleteGap(gap)}
              deleting={deletingGap === (gap.file?.replace('wiki/gaps/', '').replace('.md', '') || gap.title)}
              onUpdated={(updated) => setGaps(prev => prev.map(g => g.file === gap.file ? { ...g, ...updated } : g))}
              isExpanded={expandedGap === (gap.file || gap.title)}
              onToggle={() => setExpandedGap(prev =>
                prev === (gap.file || gap.title) ? null : (gap.file || gap.title)
              )}
            />
          ))
        )}
      </div>

      {resolveGap && (
        <ResolveModal
          gap={resolveGap}
          onClose={() => setResolveGap(null)}
          onJobsStarted={(newJobs) => setJobs(prev => [...newJobs, ...prev])}
        />
      )}
    </div>
  )
}

function DiffView({ diffLines, isNew }) {
  if (!diffLines?.length) {
    return <p className="text-muted text-xs italic font-mono">No changes recorded.</p>
  }

  return (
    <div className="font-mono text-xs leading-5 overflow-x-auto">
      {isNew && (
        <div className="mb-2 px-2 py-1 bg-success/10 text-success rounded text-xs">New file</div>
      )}
      {diffLines.map((line, i) => {
        const ch = line[0]
        let cls = 'text-white/50'
        let bg = ''
        if (ch === '+') { cls = 'text-success'; bg = 'bg-success/10' }
        else if (ch === '-') { cls = 'text-red-400'; bg = 'bg-red-500/10' }
        else if (ch === '@') { cls = 'text-accent/70'; bg = 'bg-accent/5' }

        return (
          <div key={i} className={`px-3 py-px whitespace-pre ${bg}`}>
            <span className={cls}>{line}</span>
          </div>
        )
      })}
    </div>
  )
}

function ArticlePanel({ event, activeKB, onClose }) {
  const [files, setFiles] = useState([])
  const [activeFile, setActiveFile] = useState(null)
  const [content, setContent] = useState('')
  const [fileLoading, setFileLoading] = useState(false)
  const [view, setView] = useState('diff') // 'diff' | 'article'
  const [sectionQuality, setSectionQuality] = useState({})

  useEffect(() => {
    const written = event?.files_written ?? []
    setFiles(written)
    setActiveFile(written[0] ?? null)
    // default to diff if available, else article
    setView(event?.file_diffs && Object.keys(event.file_diffs).length > 0 ? 'diff' : 'article')
  }, [event])

  useEffect(() => {
    if (!activeFile || view !== 'article') return
    setFileLoading(true)
    setContent('')
    getWikiFile(activeFile, activeKB)
      .then(d => {
        const raw = d.content ?? ''
        setContent(raw)
        setSectionQuality(parseSectionQuality(raw))
      })
      .catch(() => setContent('_Could not load file._'))
      .finally(() => setFileLoading(false))
  }, [activeFile, activeKB, view])

  const hasDiff = event.file_diffs && Object.keys(event.file_diffs).length > 0
  const activeDiff = activeFile && event.file_diffs?.[activeFile]

  // summary stats across all files for this event
  const totalAdded   = hasDiff ? Object.values(event.file_diffs).reduce((s, d) => s + (d.added ?? 0), 0) : null
  const totalRemoved = hasDiff ? Object.values(event.file_diffs).reduce((s, d) => s + (d.removed ?? 0), 0) : null

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Panel header */}
      <div className="shrink-0 flex items-center justify-between px-5 py-3 border-b border-border gap-3">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <span className={`shrink-0 px-2 py-0.5 rounded text-xs font-mono ${OP_STYLE[event.operation] || 'bg-ink-700 text-muted'}`}>
            {event.operation}
          </span>
          <span className="text-white text-sm font-medium truncate">{event.source}</span>
          {hasDiff && (
            <span className="shrink-0 text-xs font-mono">
              <span className="text-success">+{totalAdded}</span>
              <span className="text-muted mx-1">/</span>
              <span className="text-red-400">-{totalRemoved}</span>
            </span>
          )}
        </div>
        <div className="shrink-0 flex items-center gap-2">
          {/* View toggle */}
          <div className="flex rounded border border-border overflow-hidden">
            {hasDiff && (
              <button
                onClick={() => setView('diff')}
                className={`px-2.5 py-1 text-xs font-mono transition-colors ${view === 'diff' ? 'bg-accent/20 text-accent' : 'text-muted hover:text-white bg-ink-900'}`}
              >
                diff
              </button>
            )}
            <button
              onClick={() => setView('article')}
              className={`px-2.5 py-1 text-xs font-mono transition-colors ${view === 'article' ? 'bg-accent/20 text-accent' : 'text-muted hover:text-white bg-ink-900'}`}
            >
              article
            </button>
          </div>
          <button onClick={onClose} className="text-muted hover:text-white transition-colors">
            <XMarkIcon className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* File tabs */}
      {files.length > 1 && (
        <div className="shrink-0 flex gap-1 px-4 pt-2 overflow-x-auto border-b border-border pb-2">
          {files.map(f => {
            const fd = event.file_diffs?.[f]
            return (
              <button
                key={f}
                onClick={() => setActiveFile(f)}
                className={`shrink-0 px-2.5 py-1 rounded text-xs font-mono whitespace-nowrap transition-colors ${
                  activeFile === f ? 'bg-accent/20 text-accent' : 'text-muted hover:text-white bg-ink-900'
                }`}
              >
                {f.replace('wiki/', '')}
                {fd && (
                  <span className="ml-1.5 opacity-70">
                    <span className="text-success">+{fd.added}</span>
                    <span className="text-red-400 ml-0.5">-{fd.removed}</span>
                  </span>
                )}
              </button>
            )
          })}
        </div>
      )}

      {/* File path label (single file) */}
      {files.length === 1 && activeFile && (
        <div className="shrink-0 px-5 py-1.5 border-b border-border">
          <span className="text-xs font-mono text-muted">{activeFile.replace('wiki/', '')}</span>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-6 py-5 text-sm text-white/80 min-h-0">
        {files.length === 0 ? (
          <p className="text-muted text-xs italic">No files were written in this event.</p>
        ) : view === 'diff' ? (
          activeDiff
            ? <DiffView diffLines={activeDiff.diff} isNew={activeDiff.is_new} />
            : <p className="text-muted text-xs italic font-mono">No diff recorded for this event. Run a new ingest to capture diffs.</p>
        ) : fileLoading ? (
          <p className="text-muted text-sm">Loading…</p>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={makeMdComponents(sectionQuality)}>
            {preprocessWikiLinks(stripFrontmatter(content))}
          </ReactMarkdown>
        )}
      </div>
    </div>
  )
}

// ── Contamination Panel ───────────────────────────────────────────────────────

function ContaminationPanel({ activeKB, onActivityChanged }) {
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(null)
  const [toast, setToast]     = useState(null)  // {msg, ok}

  const showToast = (msg, ok = true) => {
    setToast({ msg, ok })
    setTimeout(() => setToast(null), 4000)
  }

  const load = useCallback(() => {
    setLoading(true)
    getWikiContamination(activeKB)
      .then(setData).catch(() => setData({ total: 0, pages: [] }))
      .finally(() => setLoading(false))
  }, [activeKB])

  useEffect(() => { load() }, [load])

  const handleDefragAll = async () => {
    if (!confirm('Defrag all flagged pages? The LLM will move misplaced content to the correct pages.')) return
    setRunning('defrag-all')
    try {
      await runDefrag(activeKB)
      await new Promise(r => setTimeout(r, 2000))
      load(); onActivityChanged?.()
      showToast('Defrag started — check Activity feed for results')
    } catch (e) { showToast('Defrag failed: ' + e.message, false) }
    finally { setRunning(null) }
  }

  const handleDefragPage = async (path) => {
    setRunning('defrag-' + path)
    try {
      await runDefrag(activeKB, path)
      await new Promise(r => setTimeout(r, 2000))
      load(); onActivityChanged?.()
      showToast('Defrag complete')
    } catch (e) { showToast('Defrag failed: ' + e.message, false) }
    finally { setRunning(null) }
  }

  const handleScan = async () => {
    if (!confirm('Run LLM scope scan on all pages? This makes one LLM call per entity/concept page.')) return
    setRunning('scan')
    try {
      await runScanContamination(activeKB)
      showToast('Scan running in background — results will appear in Activity feed')
      // Poll activity feed after ~30s for the completion event
      setTimeout(() => { load(); onActivityChanged?.() }, 30000)
    } catch (e) { showToast('Scan failed: ' + e.message, false) }
    finally { setRunning(null) }
  }

  const handleFalsePositive = async (path, section, belongs_on) => {
    try {
      await markFalsePositive(activeKB, path, section, belongs_on)
      load()
      showToast(`Whitelisted: "${section}" will not be flagged again`)
    } catch (e) { showToast('Failed: ' + e.message, false) }
  }

  const handleMigrateScope = async () => {
    setRunning('scope')
    try {
      await runMigrateScope(activeKB)
      // Poll after 3s — scope migration is fast (no LLM)
      await new Promise(r => setTimeout(r, 3000))
      onActivityChanged?.()
      showToast('Scope fields added — check Activity feed for details')
    } catch (e) { showToast('Migration failed: ' + e.message, false) }
    finally { setRunning(null) }
  }

  const pages = data?.pages ?? []

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Toast */}
      {toast && (
        <div className={`shrink-0 mx-4 mt-3 px-3 py-2 rounded-md text-xs font-mono border ${
          toast.ok
            ? 'bg-success/10 text-success border-success/30'
            : 'bg-red-950 text-red-400 border-red-800'
        }`}>
          {toast.msg}
        </div>
      )}

      {/* Header */}
      <div className="shrink-0 px-6 pt-4 pb-4 border-b border-border">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="font-semibold text-white text-sm">Scope Contamination</h2>
            <p className="text-xs text-muted mt-0.5">Pages with content that belongs on a different page.</p>
          </div>
          <button onClick={load} className="text-xs text-muted hover:text-white transition-colors mt-0.5">
            <ArrowPathIcon className="w-3.5 h-3.5" />
          </button>
        </div>

        {/* Action buttons */}
        <div className="flex flex-wrap gap-2 mt-3">
          {pages.length > 0 && (
            <button
              onClick={handleDefragAll}
              disabled={!!running}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium
                bg-orange-500/20 text-orange-400 border border-orange-800/50
                hover:bg-orange-500/30 transition-colors disabled:opacity-40"
            >
              {running === 'defrag-all' ? 'Defraging…' : `⚡ Defrag all (${pages.length})`}
            </button>
          )}
          <button
            onClick={handleScan}
            disabled={!!running}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium
              bg-ink-800 text-muted border border-border hover:text-white transition-colors disabled:opacity-40"
          >
            {running === 'scan' ? 'Starting…' : '🔍 Scan all pages'}
          </button>
          <button
            onClick={handleMigrateScope}
            disabled={!!running}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium
              bg-ink-800 text-muted border border-border hover:text-white transition-colors disabled:opacity-40"
          >
            {running === 'scope' ? 'Starting…' : '🏷 Add scope fields'}
          </button>
        </div>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
        {loading ? (
          <p className="text-xs text-muted">Loading…</p>
        ) : pages.length === 0 ? (
          <div className="py-12 text-center">
            <p className="text-sm text-white/60">No contamination detected</p>
            <p className="text-xs text-muted mt-1">Run a scan to check existing pages.</p>
          </div>
        ) : (
          pages.map((page, i) => (
            <div key={i} className="rounded-lg border border-orange-800/40 bg-orange-950/20 p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-semibold text-white truncate">{page.title}</p>
                  <p className="text-[10px] font-mono text-muted mt-0.5 truncate">{page.path}</p>
                  <div className="flex flex-wrap gap-1 mt-1.5">
                    {page.violations.map((v, j) => (
                      <span key={j} className="inline-flex items-center gap-1 text-[9px] pl-1.5 pr-0.5 py-0.5 rounded bg-orange-950 border border-orange-800 text-orange-400 font-mono">
                        {v.section} → {v.belongs_on}
                        <button
                          title="Mark as false positive — won't be flagged again"
                          onClick={() => handleFalsePositive(page.path, v.section, v.belongs_on)}
                          className="ml-0.5 text-orange-600 hover:text-red-400 hover:bg-orange-900 rounded px-0.5 transition-colors"
                        >✕</button>
                      </span>
                    ))}
                  </div>
                </div>
                <button
                  onClick={() => handleDefragPage(page.path)}
                  disabled={!!running}
                  className="shrink-0 px-2 py-1 text-[10px] font-mono rounded border border-orange-800/50
                    text-orange-400 hover:bg-orange-900/40 transition-colors disabled:opacity-40"
                >
                  {running === 'defrag-' + page.path ? '…' : 'defrag'}
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}

export default function Activity() {
  const { activeKB } = useAppState()
  const [events, setEvents] = useState([])
  const [loading, setLoading] = useState(true)
  const [opFilter, setOpFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [dateFilter, setDateFilter] = useState(() => new Date().toISOString().slice(0, 10))
  const [selectedEvent, setSelectedEvent] = useState(null)
  const [rightTab, setRightTab] = useState('gaps')  // 'gaps' | 'contamination'

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
      if (!dateFilter) return true
      return e.timestamp?.slice(0, 10) === dateFilter
    })
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
    <div className="h-full flex min-h-0">
      {/* Left panel — event list, fixed width */}
      <div className="w-[400px] shrink-0 flex flex-col min-h-0 border-r border-border">
        {/* Header */}
        <div className="shrink-0 px-6 pt-6 pb-4 space-y-4">
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
              {['all', 'ingest', 'gap_resolve', 'defrag', 'migrate'].map(op => (
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
            <div className="flex items-center gap-1.5">
              <input
                type="date"
                value={dateFilter}
                onChange={e => setDateFilter(e.target.value)}
                className="bg-ink-900 border border-border rounded-lg px-2 py-1.5 text-xs font-mono text-white focus:outline-none focus:border-accent [color-scheme:dark]"
              />
              {dateFilter && (
                <button
                  onClick={() => setDateFilter('')}
                  title="Clear date filter"
                  className="text-muted hover:text-white text-xs px-1 transition-colors"
                >✕</button>
              )}
            </div>
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="filter by source or page…"
              className="flex-1 min-w-32 bg-ink-900 border border-border rounded-lg px-3 py-1.5 text-xs font-mono text-white placeholder:text-muted focus:outline-none focus:border-accent"
            />
          </div>
          <span className="text-xs font-mono text-muted block">
            {filtered.length} events · <span className="text-warning">${totalCost.toFixed(4)}</span>
          </span>
        </div>

        {/* Feed */}
        <div className="flex-1 overflow-y-auto px-6 pb-6">
          {loading ? (
            <p className="text-sm text-muted">Loading…</p>
          ) : filtered.length === 0 ? (
            <div className="py-16 text-center">
              <p className="text-muted text-sm">No activity yet.</p>
              <p className="text-muted text-xs mt-1">Run an ingest or resolve a gap to start tracking changes.</p>
            </div>
          ) : (
            <div className="space-y-2">
              {filtered.map((e, i) => (
                <EventCard
                  key={i}
                  event={e}
                  selected={selectedEvent === e}
                  onSelect={ev => setSelectedEvent(prev => prev === ev ? null : ev)}
                />
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Right panel — article/diff when event selected, else tabs: Gaps | Contamination */}
      <div className="flex-1 min-h-0 bg-surface flex flex-col overflow-hidden">
        {selectedEvent ? (
          <ArticlePanel
            event={selectedEvent}
            activeKB={activeKB}
            onClose={() => setSelectedEvent(null)}
          />
        ) : (
          <>
            {/* Tab bar */}
            <div className="shrink-0 flex border-b border-border">
              {[['gaps', 'Knowledge Gaps'], ['contamination', 'Scope Contamination']].map(([id, label]) => (
                <button
                  key={id}
                  onClick={() => setRightTab(id)}
                  className={`px-4 py-2.5 text-xs font-medium transition-colors ${
                    rightTab === id
                      ? 'text-white border-b-2 border-accent -mb-px'
                      : 'text-muted hover:text-white'
                  }`}
                >
                  {id === 'contamination' && <span className="mr-1 text-orange-400">⚠</span>}
                  {label}
                </button>
              ))}
            </div>
            <div className="flex-1 min-h-0 overflow-hidden">
              {rightTab === 'gaps'
                ? <GapsPanel activeKB={activeKB} onResolved={load} />
                : <ContaminationPanel activeKB={activeKB} onActivityChanged={load} />
              }
            </div>
          </>
        )}
      </div>
    </div>
  )
}
