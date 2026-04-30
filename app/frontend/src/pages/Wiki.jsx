import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useAppState } from '../AppStateContext'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  ChevronRightIcon,
  ChevronDownIcon,
  DocumentTextIcon,
  FolderIcon,
  FolderOpenIcon,
  CheckIcon,
  ExclamationCircleIcon,
  MagnifyingGlassIcon,
  XMarkIcon,
  TrashIcon,
} from '@heroicons/react/24/outline'
import { getWikiTree, getWikiFile, saveWikiFile, deleteWikiFile, searchWiki, runDefrag } from '../api'

// ── Quality badge ─────────────────────────────────────────────────────────────

const QUALITY_COLORS = {
  0: 'bg-red-950   text-red-400   border-red-800',
  1: 'bg-red-950   text-red-400   border-red-800',
  2: 'bg-amber-950 text-amber-400 border-amber-800',
  3: 'bg-sky-950   text-sky-400   border-sky-800',
  4: 'bg-emerald-950 text-emerald-400 border-emerald-800',
}
const QUALITY_LABELS = ['Stub', 'Sparse', 'Partial', 'Adequate', 'Complete']

function QualityBadge({ score, flags }) {
  const colorClass = QUALITY_COLORS[score] ?? QUALITY_COLORS[0]
  const label = QUALITY_LABELS[score] ?? '?'
  return (
    <span
      className={`ml-1.5 inline-flex items-center text-[9px] px-1.5 py-0.5 rounded border font-mono leading-none select-none ${colorClass}`}
      title={label}
    >
      {score}/4
    </span>
  )
}

/** Parse scope_contamination block from YAML frontmatter into list of violations */
function parseScopeContamination(text) {
  const fmMatch = text.match(/^---\r?\n([\s\S]*?)\n---/)
  if (!fmMatch) return []
  const lines = fmMatch[1].split('\n')
  const violations = []
  let inSC = false
  let current = null

  for (const line of lines) {
    if (line.startsWith('scope_contamination:')) { inSC = true; continue }
    if (inSC) {
      if (!/^\s/.test(line)) { inSC = false; if (current) violations.push(current); current = null; continue }
      if (/^\s{2}-/.test(line)) {
        if (current) violations.push(current)
        current = {}
        const m = line.match(/section:\s+"?([^"]+)"?/)
        if (m) current.section = m[1]
      } else if (current) {
        const mb = line.match(/belongs_on:\s+"?([^"]+)"?/)
        if (mb) current.belongs_on = mb[1]
        const me = line.match(/excerpt:\s+"?([^"]*)"?/)
        if (me) current.excerpt = me[1]
        const mn = line.match(/is_new_page:\s+(true|false)/)
        if (mn) current.is_new_page = mn[1] === 'true'
      }
    }
  }
  if (current) violations.push(current)
  return violations
}

function ContaminationBadge({ violations, onDefrag }) {
  if (!violations?.length) return null
  const targets = [...new Set(violations.map(v => v.belongs_on).filter(Boolean))]
  return (
    <span
      className="ml-1.5 inline-flex items-center gap-1 text-[9px] px-1.5 py-0.5 rounded border
        bg-orange-950 text-orange-400 border-orange-800 font-mono leading-none cursor-pointer
        hover:bg-orange-900 transition-colors"
      title={`Scope contamination — content may belong on: ${targets.join(', ')}`}
      onClick={e => { e.stopPropagation(); onDefrag?.() }}
    >
      ⚠ mixed
    </span>
  )
}

/** Parse section_quality block from YAML frontmatter into {SectionName: {score, flags}} */
function parseSectionQuality(text) {
  const fmMatch = text.match(/^---\r?\n([\s\S]*?)\n---/)
  if (!fmMatch) return {}
  const lines = fmMatch[1].split('\n')
  const result = {}
  let inSQ = false
  let currentSection = null

  for (const line of lines) {
    if (line.startsWith('section_quality:')) {
      inSQ = true
      continue
    }
    if (inSQ) {
      if (!/^\s/.test(line)) { inSQ = false; continue }
      // Section name line: '  "SectionName":' or '  SectionName:'
      const sectionMatch = line.match(/^\s{2}"?([^":\n]+)"?:\s*$/)
      if (sectionMatch) {
        currentSection = sectionMatch[1].trim()
        result[currentSection] = { score: 0, flags: [] }
        continue
      }
      if (currentSection) {
        const scoreMatch = line.match(/^\s{4}score:\s*(\d+)/)
        if (scoreMatch) { result[currentSection].score = parseInt(scoreMatch[1]); continue }
        const flagsMatch = line.match(/^\s{4}flags:\s*\[([^\]]*)\]/)
        if (flagsMatch) {
          const fs = flagsMatch[1].trim()
          result[currentSection].flags = fs ? fs.split(',').map(s => s.trim()).filter(Boolean) : []
        }
      }
    }
  }
  return result
}

// ── Slug / index helpers ──────────────────────────────────────────────────────

function toSlug(title) {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
}

/** Recursively walk tree nodes and build { slug → path } */
function buildSlugIndex(nodes) {
  const idx = {}
  for (const node of nodes) {
    if (node.type === 'file') {
      idx[node.name.replace(/\.md$/, '')] = node.path
    } else if (node.children) {
      Object.assign(idx, buildSlugIndex(node.children))
    }
  }
  return idx
}

/** Strip YAML frontmatter (---...---) before rendering */
function stripFrontmatter(text) {
  return text.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '')
}

/** Convert [[Title]] → [Title](wikilink://slug) so react-markdown can handle it */
function preprocessWikiLinks(text) {
  return text.replace(/\[\[(.+?)\]\]/g, (_, title) => {
    const slug = toSlug(title)
    return `[${title}](wikilink://${slug})`
  })
}

// ── Markdown component factory ────────────────────────────────────────────────
// Takes slugIndex, navigate fn, and sectionQuality map for quality badges.

function buildMdComponents(slugIndex, onNavigate, sectionQuality = {}) {
  return {
    p({ children })      { return <p className="mb-3 last:mb-0 leading-relaxed">{children}</p> },
    ul({ children })     { return <ul className="list-disc list-outside ml-5 mb-3 space-y-1">{children}</ul> },
    ol({ children })     { return <ol className="list-decimal list-outside ml-5 mb-3 space-y-1">{children}</ol> },
    li({ children })     { return <li className="leading-relaxed">{children}</li> },
    strong({ children }) { return <strong className="font-semibold text-white">{children}</strong> },
    em({ children })     { return <em className="italic text-white/75">{children}</em> },
    h1({ children })     { return <h1 className="text-xl font-bold text-white mt-6 mb-3 pb-1 border-b border-border">{children}</h1> },
    h2({ children }) {
      const text = Array.isArray(children) ? children.join('') : String(children ?? '')
      const quality = sectionQuality[text]
      const flags = quality?.flags ?? []
      return (
        <div className="mt-4 mb-2">
          <h2 className="text-base font-bold text-white flex items-center">
            <span>{children}</span>
            {quality !== undefined && <QualityBadge score={quality.score} flags={flags} />}
          </h2>
          {flags.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1">
              {flags.map(f => (
                <span key={f} className="text-[9px] px-1.5 py-0.5 rounded bg-ink-800 text-muted/80 font-mono border border-border/60">
                  {f.replace(/^(missing_|no_)/, '').replace(/_/g, ' ')}
                </span>
              ))}
            </div>
          )}
        </div>
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
      // Internal wiki link
      if (href?.startsWith('wikilink://')) {
        const slug = href.slice('wikilink://'.length)
        const path = slugIndex[slug]
        if (path) {
          return (
            <button
              onClick={() => onNavigate(path)}
              className="inline-flex items-center gap-0.5 px-1.5 py-0.5 bg-accent/15 border border-accent/30
                rounded text-accent text-xs font-mono hover:bg-accent/25 transition-colors cursor-pointer"
            >
              {children}
            </button>
          )
        }
        // Link target doesn't exist yet — show as muted badge
        return (
          <span className="inline-flex items-center px-1.5 py-0.5 bg-ink-800 border border-border
            rounded text-muted text-xs font-mono" title="No wiki page found">
            {children}
          </span>
        )
      }
      // External link
      return (
        <a href={href} target="_blank" rel="noreferrer"
          className="text-accent underline underline-offset-2 hover:text-accent/80">
          {children}
        </a>
      )
    },
  }
}

// ── File tree ─────────────────────────────────────────────────────────────────

const SECTION_ICONS = { sources: '📄', entities: '🏷️', concepts: '💡', queries: '❓' }

function FileNode({ node, selectedPath, onSelect, depth = 0 }) {
  const [open, setOpen] = useState(false)   // all collapsed by default
  const isSelected = node.path === selectedPath
  const indent = depth * 12

  if (node.type === 'file') {
    const label = node.name.replace(/\.md$/, '')
    return (
      <button
        onClick={() => onSelect(node.path)}
        className={`w-full text-left flex items-center gap-1.5 px-2 py-1 rounded text-xs transition-colors
          ${isSelected ? 'bg-accent/15 text-accent' : 'text-muted hover:text-white hover:bg-ink-700'}`}
        style={{ paddingLeft: `${indent + 8}px` }}
        title={node.name}
      >
        <DocumentTextIcon className="w-3 h-3 flex-shrink-0" />
        <span className="truncate">{label}</span>
      </button>
    )
  }

  const emoji = SECTION_ICONS[node.name] || ''
  return (
    <div>
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full text-left flex items-center gap-1.5 px-2 py-1.5 rounded text-xs
          text-white/70 hover:text-white hover:bg-ink-700 transition-colors"
        style={{ paddingLeft: `${indent + 4}px` }}
      >
        {open
          ? <ChevronDownIcon className="w-3 h-3 flex-shrink-0 text-muted" />
          : <ChevronRightIcon className="w-3 h-3 flex-shrink-0 text-muted" />}
        {open
          ? <FolderOpenIcon className="w-3.5 h-3.5 flex-shrink-0 text-accent/60" />
          : <FolderIcon className="w-3.5 h-3.5 flex-shrink-0 text-muted" />}
        <span className="font-medium">{emoji} {node.name}</span>
        {node.children && (
          <span className="ml-auto text-muted/50 font-mono text-[10px]">{node.children.length}</span>
        )}
      </button>
      {open && node.children?.map((child, i) => (
        <FileNode key={i} node={child} selectedPath={selectedPath} onSelect={onSelect} depth={depth + 1} />
      ))}
    </div>
  )
}

// ── Search results ────────────────────────────────────────────────────────────

function SearchResults({ results, total, query, onSelect }) {
  if (!results) return (
    <div className="flex items-center justify-center py-8">
      <div className="flex gap-1.5">
        {[0,150,300].map(d => (
          <span key={d} className="w-1.5 h-1.5 bg-accent/60 rounded-full animate-bounce"
            style={{ animationDelay: `${d}ms` }} />
        ))}
      </div>
    </div>
  )

  if (results.length === 0) return (
    <p className="text-xs text-muted px-3 py-4 text-center">No results for "{query}"</p>
  )

  return (
    <div className="space-y-0.5">
      <p className="text-[10px] text-muted/60 px-3 py-1.5">
        {total > 50 ? `${total} results (showing 50)` : `${total} result${total !== 1 ? 's' : ''}`}
      </p>
      {results.map((r, i) => (
        <button
          key={i}
          onClick={() => onSelect(r.path)}
          className="w-full text-left px-3 py-2 rounded hover:bg-ink-700 transition-colors group"
        >
          <p className="text-xs text-white/90 truncate font-medium group-hover:text-accent">
            {r.name}
          </p>
          <p className="text-[10px] text-muted mt-0.5 line-clamp-2 leading-relaxed">
            {/* Strip ** markers for display */}
            {r.excerpt.replace(/\*\*/g, '')}
          </p>
        </button>
      ))}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Wiki() {
  const { wiki, setWiki, activeKB } = useAppState()
  const { tree, selectedPath, content, savedContent, mode, searchQuery, searchResults, searchTotal } = wiki

  const setTree         = val => setWiki(prev => ({ ...prev, tree: val }))
  const setSelectedPath = val => setWiki(prev => ({ ...prev, selectedPath: val }))
  const setContent      = val => setWiki(prev => ({ ...prev, content: val }))
  const setSavedContent = val => setWiki(prev => ({ ...prev, savedContent: val }))
  const setMode         = val => setWiki(prev => ({ ...prev, mode: val }))
  const setSearchQuery  = val => setWiki(prev => ({ ...prev, searchQuery: val }))
  const setSearchResults = val => setWiki(prev => ({ ...prev, searchResults: val }))
  const setSearchTotal  = val => setWiki(prev => ({ ...prev, searchTotal: val }))

  const [searchParams, setSearchParams] = useSearchParams()
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveStatus, setSaveStatus] = useState(null)
  const [deleting, setDeleting] = useState(false)
  const [searching, setSearching] = useState(false)
  const searchTimerRef = useRef(null)

  const dirty = content !== savedContent

  // Slug index for wikilink resolution
  const slugIndex = useMemo(() => buildSlugIndex(tree), [tree])

  // Load tree when active KB changes (or on mount)
  useEffect(() => {
    getWikiTree(activeKB).then(d => setTree(d.tree)).catch(console.error)
  }, [activeKB])

  // Load file when selection changes
  useEffect(() => {
    if (!selectedPath) return
    setLoading(true)
    setSearchParams({ file: selectedPath })
    getWikiFile(selectedPath, activeKB)
      .then(d => { setContent(d.content); setSavedContent(d.content); setSaveStatus(null) })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [selectedPath])

  const handleSelect = useCallback(path => {
    if (dirty && !confirm('You have unsaved changes. Discard and open new file?')) return
    setSelectedPath(path)
    setMode('preview')
  }, [dirty])

  // Debounced search
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults(null)
      return
    }
    clearTimeout(searchTimerRef.current)
    setSearching(true)
    searchTimerRef.current = setTimeout(() => {
      searchWiki(searchQuery.trim(), activeKB)
        .then(d => { setSearchResults(d.results); setSearchTotal(d.total) })
        .catch(console.error)
        .finally(() => setSearching(false))
    }, 300)
    return () => clearTimeout(searchTimerRef.current)
  }, [searchQuery])

  const handleDelete = useCallback(async () => {
    if (!selectedPath) return
    if (!confirm(`Delete "${selectedPath.split('/').pop()}"? This cannot be undone.`)) return
    setDeleting(true)
    try {
      await deleteWikiFile(selectedPath, activeKB)
      setSelectedPath(null)
      setContent('')
      setSavedContent('')
      getWikiTree(activeKB).then(d => setTree(d.tree)).catch(console.error)
    } catch (e) {
      console.error(e)
      alert('Delete failed: ' + e.message)
    } finally {
      setDeleting(false)
    }
  }, [selectedPath, activeKB])

  const handleSave = useCallback(async () => {
    if (!selectedPath || !dirty) return
    setSaving(true); setSaveStatus(null)
    try {
      await saveWikiFile(selectedPath, content, activeKB)
      setSavedContent(content); setSaveStatus('saved')
      setTimeout(() => setSaveStatus(null), 2500)
    } catch (e) {
      console.error(e); setSaveStatus('error')
    } finally {
      setSaving(false)
    }
  }, [selectedPath, content, dirty])

  // ⌘S / Ctrl+S to save
  useEffect(() => {
    const handler = e => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); handleSave() }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [handleSave])

  // Parse section quality + scope contamination from current file's frontmatter
  const sectionQuality    = useMemo(() => parseSectionQuality(content),    [content])
  const contaminations    = useMemo(() => parseScopeContamination(content), [content])
  const [defraging, setDefraging] = useState(false)

  const handleDefrag = useCallback(async () => {
    if (!selectedPath) return
    if (!confirm(`Defrag "${selectedPath.split('/').pop()}"? The LLM will move misplaced content to the correct pages.`)) return
    setDefraging(true)
    try {
      await runDefrag(activeKB, selectedPath)
      // Reload the file after defrag
      const d = await getWikiFile(selectedPath, activeKB)
      setContent(d.content); setSavedContent(d.content)
    } catch (e) {
      alert('Defrag failed: ' + e.message)
    } finally {
      setDefraging(false)
    }
  }, [selectedPath, activeKB])

  // Memoised markdown components with wikilink navigation and quality badges
  const mdComponents = useMemo(
    () => buildMdComponents(slugIndex, handleSelect, sectionQuality),
    [slugIndex, handleSelect, sectionQuality]
  )

  const fileName = selectedPath ? selectedPath.split('/').pop() : null

  return (
    <div className="h-full flex">

      {/* ── Sidebar ──────────────────────────────────────────────────────────── */}
      <aside className="w-60 flex-shrink-0 flex flex-col border-r border-border bg-ink-900 overflow-hidden">

        {/* Search input */}
        <div className="px-3 py-3 border-b border-border">
          <div className="relative flex items-center">
            <MagnifyingGlassIcon className="w-3.5 h-3.5 text-muted absolute left-2.5 pointer-events-none" />
            <input
              type="text"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              placeholder="Search wiki…"
              className="w-full pl-7 pr-7 py-1.5 bg-ink-800 border border-border rounded-md text-xs
                text-white placeholder:text-muted focus:outline-none focus:border-accent transition-colors"
            />
            {searchQuery && (
              <button onClick={() => { setSearchQuery(''); setSearchResults(null) }}
                className="absolute right-2 text-muted hover:text-white transition-colors">
                <XMarkIcon className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        </div>

        {/* Tree or search results */}
        <div className="flex-1 overflow-y-auto py-2 px-1">
          {searchQuery ? (
            <SearchResults
              results={searching ? null : searchResults}
              total={searchTotal}
              query={searchQuery}
              onSelect={handleSelect}
            />
          ) : tree.length === 0 ? (
            <p className="text-xs text-muted px-3 py-2">Loading…</p>
          ) : (
            tree.map((node, i) => (
              <FileNode key={i} node={node} selectedPath={selectedPath} onSelect={handleSelect} depth={0} />
            ))
          )}
        </div>
      </aside>

      {/* ── Editor / Preview pane ────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col overflow-hidden">

        {/* Toolbar */}
        <div className="flex items-center gap-3 px-6 py-3 border-b border-border bg-ink-900 flex-shrink-0">
          {fileName ? (
            <>
              <span className="text-xs font-mono text-muted truncate max-w-xs" title={selectedPath}>
                {selectedPath}
              </span>
              {dirty && <span className="w-1.5 h-1.5 rounded-full bg-amber-400 flex-shrink-0" title="Unsaved changes" />}
            </>
          ) : (
            <span className="text-xs text-muted">Select a file from the tree</span>
          )}

          <div className="ml-auto flex items-center gap-2">
            {selectedPath && (
              <div className="flex rounded-md border border-border overflow-hidden text-xs">
                {['preview', 'edit'].map(m => (
                  <button key={m} onClick={() => setMode(m)}
                    className={`px-3 py-1.5 transition-colors capitalize
                      ${mode === m ? 'bg-accent/20 text-accent' : 'text-muted hover:text-white hover:bg-ink-700'}`}>
                    {m}
                  </button>
                ))}
              </div>
            )}
            {selectedPath && mode === 'edit' && (
              <button onClick={handleSave} disabled={!dirty || saving}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium
                  disabled:opacity-40 disabled:cursor-not-allowed
                  bg-accent/20 hover:bg-accent/30 text-accent border border-accent/30 transition-colors">
                {saveStatus === 'saved' ? <><CheckIcon className="w-3 h-3" /> Saved</>
                  : saveStatus === 'error' ? <><ExclamationCircleIcon className="w-3 h-3 text-red-400" /> Error</>
                  : saving ? 'Saving…'
                  : <>Save <span className="text-white/30 font-mono ml-1">⌘S</span></>}
              </button>
            )}
            {selectedPath && contaminations.length > 0 && (
              <button
                onClick={handleDefrag}
                disabled={defraging}
                title={`Scope contamination — content may belong on: ${[...new Set(contaminations.map(v => v.belongs_on))].join(', ')}`}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium
                  text-orange-400/80 hover:text-orange-400 hover:bg-orange-950/40 border border-transparent
                  hover:border-orange-800/50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <span className="text-[10px]">⚠</span>
                {defraging ? 'Defraging…' : `Mixed (${contaminations.length})`}
              </button>
            )}
            {selectedPath && (
              <button
                onClick={handleDelete}
                disabled={deleting}
                title="Delete this page"
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-xs font-medium
                  text-red-400/70 hover:text-red-400 hover:bg-red-950/30 border border-transparent
                  hover:border-red-800/40 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                <TrashIcon className="w-3.5 h-3.5" />
                {deleting ? 'Deleting…' : 'Delete'}
              </button>
            )}
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-auto">
          {!selectedPath ? (
            <div className="flex flex-col items-center justify-center h-full text-center px-8">
              <FolderOpenIcon className="w-12 h-12 text-muted/40 mb-4" />
              <p className="text-white/60 text-sm mb-1">No file selected</p>
              <p className="text-xs text-muted">Pick a file from the tree or search above</p>
            </div>
          ) : loading ? (
            <div className="flex items-center justify-center h-full">
              <div className="flex gap-1.5">
                {[0,150,300].map(d => (
                  <span key={d} className="w-2 h-2 bg-accent/60 rounded-full animate-bounce"
                    style={{ animationDelay: `${d}ms` }} />
                ))}
              </div>
            </div>
          ) : mode === 'preview' ? (
            <div className="max-w-3xl mx-auto px-8 py-8 text-sm text-white/90">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents} urlTransform={u => u}>
                {preprocessWikiLinks(stripFrontmatter(content))}
              </ReactMarkdown>
            </div>
          ) : (
            <textarea
              value={content}
              onChange={e => setContent(e.target.value)}
              spellCheck={false}
              className="w-full h-full resize-none bg-transparent text-white/90 font-mono text-xs
                leading-relaxed px-8 py-8 focus:outline-none caret-accent"
              placeholder="Start typing markdown…"
            />
          )}
        </div>
      </div>
    </div>
  )
}
