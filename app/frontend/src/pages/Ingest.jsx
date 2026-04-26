import { useState, useRef, useEffect } from 'react'
import { ArrowUpTrayIcon, DocumentTextIcon, ChevronDownIcon, ChevronRightIcon } from '@heroicons/react/24/outline'
import { ingestFile, ingestUrl, getWikiGaps, resolveJobStatus, resolveAll, resolveBatchStatus, deleteGap, updateGap, createGap } from '../api'
import CostBadge from '../components/CostBadge'
import ResolveModal from '../components/ResolveModal'
import { useAppState } from '../AppStateContext'

// ── Diff viewer ────────────────────────────────────────────────────────────────

function DiffLine({ line }) {
  if (line.startsWith('+++') || line.startsWith('---')) {
    return <div className="text-muted font-mono text-xs py-px px-3 select-none">{line}</div>
  }
  if (line.startsWith('@@')) {
    return <div className="text-accent/70 font-mono text-xs py-px px-3 bg-accent/5 select-none">{line}</div>
  }
  if (line.startsWith('+')) {
    return (
      <div className="flex font-mono text-xs bg-green-950/40 border-l-2 border-green-500">
        <span className="w-5 text-green-500/60 select-none flex-shrink-0 text-center">+</span>
        <span className="text-green-300 py-px pr-3 whitespace-pre-wrap break-all">{line.slice(1)}</span>
      </div>
    )
  }
  if (line.startsWith('-')) {
    return (
      <div className="flex font-mono text-xs bg-red-950/40 border-l-2 border-red-500">
        <span className="w-5 text-red-500/60 select-none flex-shrink-0 text-center">−</span>
        <span className="text-red-300 py-px pr-3 whitespace-pre-wrap break-all">{line.slice(1)}</span>
      </div>
    )
  }
  return (
    <div className="flex font-mono text-xs text-muted/60 border-l-2 border-transparent">
      <span className="w-5 select-none flex-shrink-0"> </span>
      <span className="py-px pr-3 whitespace-pre-wrap break-all">{line}</span>
    </div>
  )
}

function FileDiff({ diff }) {
  const [open, setOpen] = useState(false)
  const isNew = diff.is_new
  const hasDiff = diff.diff && diff.diff.length > 0

  const opLabel = isNew ? 'new' : diff.op
  const opColor = isNew
    ? 'text-green-400 bg-green-950/40 border-green-800/50'
    : diff.op === 'append'
      ? 'text-blue-400 bg-blue-950/40 border-blue-800/50'
      : 'text-yellow-400 bg-yellow-950/40 border-yellow-800/50'

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      {/* Header row */}
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-3 px-4 py-3 bg-ink-800 hover:bg-ink-700 transition-colors text-left"
      >
        {open
          ? <ChevronDownIcon className="w-3.5 h-3.5 text-muted flex-shrink-0" />
          : <ChevronRightIcon className="w-3.5 h-3.5 text-muted flex-shrink-0" />
        }

        {/* Op badge */}
        <span className={`px-1.5 py-0.5 text-xs font-mono rounded border ${opColor}`}>
          {opLabel}
        </span>

        {/* Path */}
        <span className="font-mono text-xs text-white/80 flex-1 truncate">{diff.path}</span>

        {/* +/- counts */}
        <span className="flex items-center gap-2 flex-shrink-0 ml-2">
          {diff.added > 0 && (
            <span className="font-mono text-xs text-green-400">+{diff.added}</span>
          )}
          {diff.removed > 0 && (
            <span className="font-mono text-xs text-red-400">−{diff.removed}</span>
          )}
        </span>
      </button>

      {/* Diff body */}
      {open && (
        <div className="bg-ink-900 max-h-72 overflow-y-auto">
          {hasDiff ? (
            diff.diff.map((line, i) => <DiffLine key={i} line={line} />)
          ) : (
            <p className="text-xs text-muted font-mono px-4 py-3 italic">No diff available</p>
          )}
        </div>
      )}
    </div>
  )
}

// ── Job status card ────────────────────────────────────────────────────────────

function JobCard({ job, onUpdate }) {
  useEffect(() => {
    if (job.status !== 'running') return
    const iv = setInterval(async () => {
      try {
        const data = await resolveJobStatus(job.job_id)
        if (data.status !== 'running') {
          onUpdate(job.job_id, data)
          clearInterval(iv)
        }
      } catch { clearInterval(iv) }
    }, 3000)
    return () => clearInterval(iv)
  }, [job.status])

  const colors = {
    running: 'bg-blue-950/20 border-blue-800/30 text-blue-300',
    done:    'bg-green-950/20 border-green-800/30 text-green-300',
    error:   'bg-red-950/20 border-red-800/30 text-red-300',
  }
  const spinnerColors = { running: 'text-blue-400', done: '', error: '' }

  return (
    <div className={`p-3 rounded-lg border text-xs ${colors[job.status] || colors.error}`}>
      <div className="flex items-center gap-2 mb-0.5">
        {job.status === 'running' && (
          <svg className={`animate-spin w-3 h-3 flex-shrink-0 ${spinnerColors.running}`} fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
        )}
        {job.status === 'done'  && <span className="text-green-400 flex-shrink-0">✓</span>}
        {job.status === 'error' && <span className="text-red-400 flex-shrink-0">✗</span>}
        <p className="font-medium truncate">{job.title}</p>
      </div>
      {job.status === 'running' && (
        <p className="text-muted ml-5">Fetching article + running ingest pipeline…</p>
      )}
      {job.status === 'done' && job.result && (
        <p className="text-muted ml-5">
          {job.result.files_written?.length || 0} files written
          {job.result.cost_usd != null ? ` · $${job.result.cost_usd.toFixed(4)}` : ''}
        </p>
      )}
      {job.status === 'error' && (
        <p className="text-red-400/70 ml-5 truncate">{job.error}</p>
      )}
    </div>
  )
}

// ── New KG inline form ────────────────────────────────────────────────────────

function NewGapForm({ activeKB, onCreated, onCancel }) {
  const [title, setTitle]       = useState('')
  const [chipInput, setChipInput] = useState('')
  const [sections, setSections] = useState([])
  const [saving, setSaving]     = useState(false)
  const [error, setError]       = useState(null)

  const addChip = () => {
    const val = chipInput.trim()
    if (val && !sections.includes(val)) setSections(prev => [...prev, val])
    setChipInput('')
  }

  const handleChipKey = (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addChip() }
    if (e.key === 'Backspace' && !chipInput && sections.length) {
      setSections(prev => prev.slice(0, -1))
    }
  }

  const handleSave = async () => {
    const finalSections = chipInput.trim()
      ? [...sections, chipInput.trim()]
      : sections
    if (!title.trim() || !finalSections.length) {
      setError('Title and at least one section required.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const result = await createGap(title.trim(), finalSections, activeKB)
      onCreated({
        file: result.file,
        title: result.title,
        referenced_page: result.referenced_page,
        missing_sections: result.missing_sections,
      })
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="p-4 bg-amber-950/10 border border-amber-700/40 rounded-xl space-y-3">
      <p className="text-xs font-mono text-amber-400 uppercase tracking-wider">New Knowledge Gap</p>

      <input
        autoFocus
        value={title}
        onChange={e => setTitle(e.target.value)}
        onKeyDown={e => e.key === 'Escape' && onCancel()}
        placeholder="Gap title, e.g. MAP target in vasopressor shock"
        className="w-full bg-ink-900 border border-border rounded px-3 py-1.5 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-amber-600/60"
      />

      {/* Chip input */}
      <div
        className="flex flex-wrap gap-1 min-h-[34px] bg-ink-900 border border-border rounded px-2 py-1 cursor-text focus-within:border-amber-600/60"
        onClick={() => document.getElementById('gap-chip-input')?.focus()}
      >
        {sections.map((s, i) => (
          <span key={i} className="inline-flex items-center gap-1 px-1.5 py-0.5 text-xs bg-amber-900/30 border border-amber-800/30 rounded text-amber-400/80 font-mono">
            {s}
            <button onClick={() => setSections(prev => prev.filter((_, j) => j !== i))} className="text-amber-700 hover:text-red-400 leading-none">×</button>
          </span>
        ))}
        <input
          id="gap-chip-input"
          value={chipInput}
          onChange={e => setChipInput(e.target.value)}
          onKeyDown={handleChipKey}
          onBlur={addChip}
          placeholder={sections.length ? '' : 'Add sections… (Enter to add each)'}
          className="flex-1 min-w-[140px] bg-transparent text-xs text-white placeholder:text-muted/40 focus:outline-none py-0.5"
        />
      </div>

      {error && <p className="text-xs text-red-400">{error}</p>}

      <div className="flex gap-2 justify-end">
        <button onClick={onCancel} className="px-3 py-1 text-xs border border-border rounded text-muted hover:text-white transition-colors">
          Cancel
        </button>
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-3 py-1 text-xs bg-amber-800/40 hover:bg-amber-800/60 border border-amber-700/40 rounded text-amber-300 font-medium transition-colors disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save Gap'}
        </button>
      </div>
    </div>
  )
}

// ── Gap card with inline editing ──────────────────────────────────────────────

function GapCard({ gap, activeKB, onResolve, onDelete, deleting, onUpdated }) {
  const stem = gap.file?.replace('wiki/gaps/', '').replace('.md', '') || gap.title

  const [editingTitle, setEditingTitle] = useState(false)
  const [titleVal, setTitleVal]         = useState(gap.title)
  const [sections, setSections]         = useState(gap.missing_sections || [])
  const [editingIdx, setEditingIdx]     = useState(null)
  const [editingVal, setEditingVal]     = useState('')
  const [saving, setSaving]             = useState(false)

  const save = async (newTitle, newSections) => {
    setSaving(true)
    try {
      await updateGap(stem, { title: newTitle, missing_sections: newSections }, activeKB)
      onUpdated({ title: newTitle, missing_sections: newSections })
    } catch (e) {
      console.error(e)
    } finally {
      setSaving(false)
    }
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
    setSections(next)
    save(titleVal, next)
  }

  const removeChip = (idx) => {
    const next = sections.filter((_, i) => i !== idx)
    setSections(next)
    save(titleVal, next)
  }

  return (
    <div className="p-4 bg-amber-950/20 border border-amber-800/30 rounded-xl">
      {/* Header row */}
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex-1 min-w-0">
          {editingTitle ? (
            <input
              autoFocus
              value={titleVal}
              onChange={e => setTitleVal(e.target.value)}
              onBlur={commitTitle}
              onKeyDown={e => { if (e.key === 'Enter') commitTitle(); if (e.key === 'Escape') { setEditingTitle(false); setTitleVal(gap.title) } }}
              className="w-full bg-amber-950/40 border border-amber-700/50 rounded px-2 py-0.5 text-sm font-semibold text-amber-300 focus:outline-none focus:border-amber-500"
            />
          ) : (
            <div className="flex items-center gap-1.5 group">
              <p className="text-sm font-semibold text-amber-300">{titleVal}</p>
              <button
                onClick={() => setEditingTitle(true)}
                className="opacity-0 group-hover:opacity-100 text-amber-600 hover:text-amber-400 transition-all text-[10px] border border-amber-800/40 rounded px-1 py-0.5"
              >
                edit
              </button>
            </div>
          )}
        </div>
        <div className="flex items-center gap-1.5 flex-shrink-0">
          <button
            onClick={onResolve}
            className="px-2.5 py-1 text-xs bg-accent/20 hover:bg-accent/40 border border-accent/30 rounded-md text-accent transition-colors font-medium"
          >
            Resolve
          </button>
          <button
            onClick={onDelete}
            disabled={deleting}
            className="w-6 h-6 flex items-center justify-center rounded text-muted hover:text-red-400 hover:bg-red-950/30 border border-transparent hover:border-red-800/40 transition-all disabled:opacity-40 text-sm leading-none"
            title="Delete gap"
          >
            ×
          </button>
        </div>
      </div>

      <p className="text-xs text-muted font-mono mb-2">{gap.referenced_page}</p>

      {/* Section chips */}
      <div className="flex flex-wrap gap-1 items-center">
        {sections.map((s, j) => (
          editingIdx === j ? (
            <input
              key={j}
              autoFocus
              value={editingVal}
              onChange={e => setEditingVal(e.target.value)}
              onBlur={() => commitChip(j)}
              onKeyDown={e => { if (e.key === 'Enter') commitChip(j); if (e.key === 'Escape') setEditingIdx(null) }}
              className="px-1.5 py-0.5 text-xs bg-amber-900/50 border border-amber-600/50 rounded text-amber-300 font-mono focus:outline-none min-w-[120px]"
            />
          ) : (
            <span
              key={j}
              className="group inline-flex items-center gap-1 px-1.5 py-0.5 text-xs bg-amber-900/30 border border-amber-800/30 rounded text-amber-400/80 font-mono"
            >
              {s}
              <button
                onClick={() => { setEditingIdx(j); setEditingVal(s) }}
                className="opacity-0 group-hover:opacity-100 text-amber-600 hover:text-amber-300 transition-all leading-none"
                title="Edit"
              >
                ✎
              </button>
              <button
                onClick={() => removeChip(j)}
                className="opacity-0 group-hover:opacity-100 text-amber-700 hover:text-red-400 transition-all leading-none"
                title="Remove"
              >
                ×
              </button>
            </span>
          )
        ))}
        {saving && <span className="text-[10px] text-muted/60 font-mono ml-1">saving…</span>}
      </div>
    </div>
  )
}

// ── Main page ──────────────────────────────────────────────────────────────────

export default function Ingest() {
  const { ingest, setIngest } = useAppState()
  const { tab, file, pmid: urlInput, result, error } = ingest
  const setTab      = val => setIngest(prev => ({ ...prev, tab: val }))
  const setFile     = val => setIngest(prev => ({ ...prev, file: val }))
  const setUrlInput = val => setIngest(prev => ({ ...prev, pmid: val }))
  const setResult   = val => setIngest(prev => ({ ...prev, result: val }))
  const setError    = val => setIngest(prev => ({ ...prev, error: val }))

  const [dragging, setDragging] = useState(false)
  const [loading, setLoading]   = useState(false)
  const inputRef = useRef()

  const handleDrop = (e) => {
    e.preventDefault()
    setDragging(false)
    const f = e.dataTransfer.files[0]
    if (f) setFile(f)
  }

  const { activeKB } = useAppState()

  const [gaps, setGaps]               = useState([])
  const [deletingGap, setDeletingGap] = useState(null)
  const [resolveGap, setResolveGap]   = useState(null)
  const [showNewGap, setShowNewGap]   = useState(false)
  const [jobs, setJobs]               = useState([])
  const [batchId, setBatchId]         = useState(null)
  const [batchInfo, setBatchInfo]     = useState(null)
  const addedJobIds                   = useRef(new Set())

  useEffect(() => {
    getWikiGaps(activeKB).then(d => setGaps(d.gaps || [])).catch(() => setGaps([]))
  }, [activeKB])

  const refreshGaps = () => {
    getWikiGaps(activeKB).then(d => setGaps(d.gaps || [])).catch(() => {})
  }

  const handleJobsStarted = (newJobs) => {
    setJobs(prev => [...newJobs, ...prev])
  }

  const handleJobUpdate = (jobId, data) => {
    setJobs(prev => prev.map(j => j.job_id === jobId ? { ...j, ...data } : j))
    if (data.status === 'done') refreshGaps()
  }

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
        if (data.status === 'done') {
          clearInterval(iv)
          setBatchId(null)
        }
      } catch { clearInterval(iv) }
    }, 3000)
    return () => clearInterval(iv)
  }, [batchId])

  const handleDeleteGap = async (gap) => {
    const stem = gap.file?.replace('wiki/gaps/', '').replace('.md', '') || gap.title
    setDeletingGap(stem)
    try {
      await deleteGap(stem, activeKB)
      setGaps(prev => prev.filter(g => g.file !== gap.file))
    } catch (err) {
      console.error(err)
    } finally {
      setDeletingGap(null)
    }
  }

  const handleResolveAll = async () => {
    try {
      addedJobIds.current = new Set()
      const data = await resolveAll(activeKB)
      setBatchId(data.batch_id)
      setBatchInfo({ total_gaps: gaps.length, completed_gaps: 0, status: 'running' })
    } catch (e) {
      console.error('resolve-all failed:', e)
    }
  }

  const handleIngestFile = async () => {
    if (!file) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await ingestFile(file, activeKB)
      setResult(data)
      refreshGaps()
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleIngestUrl = async () => {
    if (!urlInput.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await ingestUrl(urlInput.trim(), activeKB)
      setResult(data)
      refreshGaps()
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
    <div className="h-full flex">
      {/* Left: Input */}
      <div className="w-1/2 border-r border-border p-8 flex flex-col gap-6">
        <div>
          <h1 className="font-display text-2xl font-semibold text-white mb-1">Ingest Source</h1>
          <p className="text-sm text-muted">Add a document to the wiki. Claude will extract and file it.</p>
        </div>

        {/* Tab switcher */}
        <div className="flex gap-1 p-1 bg-ink-800 rounded-lg w-fit">
          {['file', 'url'].map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-1.5 text-sm rounded-md transition-all ${
                tab === t ? 'bg-accent text-white' : 'text-muted hover:text-white'
              }`}
            >
              {t === 'file' ? 'Upload File' : 'From URL'}
            </button>
          ))}
        </div>

        {tab === 'file' ? (
          <div className="flex flex-col gap-4">
            <div
              onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
              onDragLeave={() => setDragging(false)}
              onDrop={handleDrop}
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-10 flex flex-col items-center justify-center cursor-pointer transition-all ${
                dragging
                  ? 'border-accent bg-accent/10'
                  : 'border-border hover:border-accent/50 hover:bg-accent/5'
              }`}
            >
              <ArrowUpTrayIcon className="w-8 h-8 text-muted mb-3" />
              {file ? (
                <div className="flex items-center gap-2">
                  <DocumentTextIcon className="w-4 h-4 text-accent" />
                  <span className="text-sm text-white font-mono">{file.name}</span>
                </div>
              ) : (
                <>
                  <p className="text-sm text-white mb-1">Drop a file or click to browse</p>
                  <p className="text-xs text-muted">.pdf, .md, .txt accepted</p>
                </>
              )}
              <input
                ref={inputRef}
                type="file"
                accept=".pdf,.md,.txt"
                className="hidden"
                onChange={e => setFile(e.target.files[0])}
              />
            </div>
            <button
              onClick={handleIngestFile}
              disabled={!file || loading}
              className="px-6 py-2.5 bg-accent hover:bg-accent-dim disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition-colors"
            >
              {loading ? (
                <span className="flex items-center gap-2 justify-center">
                  <svg className="animate-spin w-4 h-4" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
                  </svg>
                  Processing with Claude...
                </span>
              ) : 'Ingest'}
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <div>
              <input
                type="url"
                value={urlInput}
                onChange={e => setUrlInput(e.target.value)}
                onKeyDown={e => e.key === 'Enter' && handleIngestUrl()}
                placeholder="https://www.ncbi.nlm.nih.gov/books/NBK470195/"
                className="w-full px-4 py-2.5 bg-ink-800 border border-border rounded-lg text-white text-sm placeholder:text-muted focus:outline-none focus:border-accent transition-colors"
              />
              <p className="text-xs text-muted mt-1.5">
                Any public URL — NCBI bookshelf, Wikipedia, journal articles, blog posts, guidelines…
                Images on the page will be vision-transcribed automatically.
              </p>
            </div>
            <button
              onClick={handleIngestUrl}
              disabled={!urlInput.trim() || loading}
              className="px-6 py-2.5 bg-accent hover:bg-accent-dim disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-medium rounded-lg transition-colors"
            >
              {loading ? (
                <span className="flex items-center gap-2 justify-center">
                  <svg className="animate-spin w-4 h-4" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
                  </svg>
                  Fetching & Processing...
                </span>
              ) : 'Fetch & Ingest'}
            </button>
          </div>
        )}

        {error && (
          <div className="p-4 bg-red-950/50 border border-red-800 rounded-lg text-sm text-red-400">
            {error}
          </div>
        )}
      </div>

      {/* Right: Results */}
      <div className="w-1/2 p-8 overflow-y-auto flex flex-col gap-6">
        {result ? (
          <>
            <div>
              <h2 className="font-display text-lg font-semibold text-white mb-1">Result</h2>
              <p className="text-xs text-muted">Ingest complete</p>
            </div>

            {/* Summary */}
            <div className="p-5 bg-surface border border-border rounded-xl">
              <p className="text-xs font-mono text-accent uppercase tracking-wider mb-3">Summary</p>
              <p className="text-sm text-white/90 leading-relaxed">{result.summary}</p>
            </div>

            {/* Diff viewer */}
            {result.diffs?.length > 0 && (
              <div>
                <p className="text-xs font-mono text-accent uppercase tracking-wider mb-3">
                  Changes — {result.diffs.length} files
                </p>
                <div className="flex flex-col gap-2">
                  {result.diffs.map((diff, i) => (
                    <FileDiff key={i} diff={diff} />
                  ))}
                </div>
              </div>
            )}

            {result.files_written?.length === 0 && (
              <div className="p-4 bg-yellow-950/40 border border-yellow-800/50 rounded-xl">
                <p className="text-xs font-mono text-yellow-400 uppercase tracking-wider mb-1">No files written</p>
                <p className="text-xs text-muted">Claude may have refused or the response was truncated. Check the summary.</p>
              </div>
            )}

            {/* Gap files written */}
            {result.gap_files_written?.length > 0 && (
              <div className="p-4 bg-amber-950/20 border border-amber-800/40 rounded-xl">
                <p className="text-xs font-mono text-amber-400/80 uppercase tracking-wider mb-2">
                  Gap Files Updated — {result.gap_files_written.length} files in wiki/gaps/
                </p>
                <div className="flex flex-col gap-0.5">
                  {result.gap_files_written.map((f, i) => (
                    <p key={i} className="text-xs font-mono text-amber-300/60">{f}</p>
                  ))}
                </div>
              </div>
            )}

            {/* Knowledge Gaps */}
            {result.knowledge_gaps?.length > 0 && (
              <div className="p-4 bg-amber-950/30 border border-amber-800/50 rounded-xl">
                <p className="text-xs font-mono text-amber-400 uppercase tracking-wider mb-3">
                  Knowledge Gaps — {result.knowledge_gaps.length} pages need more sources
                </p>
                <div className="flex flex-col gap-2">
                  {result.knowledge_gaps.map((gap, i) => (
                    <div key={i}>
                      <p className="text-xs font-mono text-amber-300/80 mb-1">{gap.page}</p>
                      <div className="flex flex-wrap gap-1">
                        {gap.missing_sections.map((s, j) => (
                          <span key={j} className="px-1.5 py-0.5 text-xs bg-amber-900/40 border border-amber-800/40 rounded text-amber-400/80 font-mono">
                            {s}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Errors */}
            {result.errors?.length > 0 && (
              <div className="p-4 bg-red-950/40 border border-red-800/50 rounded-xl">
                <p className="text-xs font-mono text-red-400 uppercase tracking-wider mb-2">Errors</p>
                {result.errors.map((e, i) => (
                  <p key={i} className="text-xs font-mono text-red-300">{e}</p>
                ))}
              </div>
            )}

            <CostBadge
              inputTokens={result.input_tokens}
              outputTokens={result.output_tokens}
              costUsd={result.cost_usd}
              model={result.model}
            />
          </>
        ) : (
          <div className="flex-1 flex flex-col gap-4">
            {/* Active resolve jobs — always at top */}
            {jobs.length > 0 && (
              <div>
                <p className="text-xs font-mono text-blue-400 uppercase tracking-wider mb-2">
                  Active Resolutions — {jobs.length}
                </p>
                <div className="flex flex-col gap-2">
                  {jobs.map(job => (
                    <JobCard key={job.job_id} job={job} onUpdate={handleJobUpdate} />
                  ))}
                </div>
              </div>
            )}

            {gaps.length > 0 || showNewGap ? (
              <>
                <div>
                  <div className="flex items-center justify-between mb-1">
                    <p className="text-xs font-mono text-amber-400 uppercase tracking-wider">
                      Pending Knowledge Gaps{gaps.length > 0 ? ` — ${gaps.length} pages` : ''}
                    </p>
                    <div className="flex items-center gap-1.5">
                      <button
                        onClick={() => setShowNewGap(v => !v)}
                        className="px-2.5 py-1 text-xs bg-amber-900/20 hover:bg-amber-900/40 border border-amber-700/30 rounded-md text-amber-400 transition-colors font-medium"
                      >
                        + New KG
                      </button>
                      {gaps.length > 0 && (
                        <button
                          onClick={handleResolveAll}
                          disabled={!!batchId}
                          className="flex-shrink-0 px-2.5 py-1 text-xs bg-blue-900/20 hover:bg-blue-900/40 border border-blue-700/30 rounded-md text-blue-400 transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          {batchId
                            ? `Resolving… ${batchInfo?.completed_gaps ?? 0}/${batchInfo?.total_gaps ?? gaps.length}`
                            : 'Resolve All'}
                        </button>
                      )}
                    </div>
                  </div>
                  <p className="text-xs text-muted">
                    These sections are missing from your wiki. Ingest a relevant source to fill them.
                  </p>
                </div>
                {showNewGap && (
                  <NewGapForm
                    activeKB={activeKB}
                    onCreated={(gap) => { setGaps(prev => [gap, ...prev]); setShowNewGap(false) }}
                    onCancel={() => setShowNewGap(false)}
                  />
                )}
                <div className="flex flex-col gap-3 overflow-y-auto">
                  {gaps.map((gap, i) => (
                    <GapCard
                      key={gap.file || i}
                      gap={gap}
                      activeKB={activeKB}
                      onResolve={() => setResolveGap(gap)}
                      onDelete={() => handleDeleteGap(gap)}
                      deleting={deletingGap === (gap.file?.replace('wiki/gaps/', '').replace('.md', '') || gap.title)}
                      onUpdated={(updated) => setGaps(prev => prev.map(g => g.file === gap.file ? { ...g, ...updated } : g))}
                    />
                  ))}
                </div>
              </>
            ) : (
              <div className="flex-1 flex flex-col items-center justify-center text-center">
                <div className="w-16 h-16 rounded-full bg-ink-800 flex items-center justify-center mb-4">
                  <DocumentTextIcon className="w-7 h-7 text-muted" />
                </div>
                <p className="text-sm text-muted mb-4">Results will appear here after ingestion</p>
                <button
                  onClick={() => setShowNewGap(true)}
                  className="px-3 py-1.5 text-xs bg-amber-900/20 hover:bg-amber-900/40 border border-amber-700/30 rounded-md text-amber-400 transition-colors font-medium"
                >
                  + Add Knowledge Gap
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>

    {resolveGap && (
      <ResolveModal
        gap={resolveGap}
        onClose={() => setResolveGap(null)}
        onJobsStarted={handleJobsStarted}
      />
    )}
    </>
  )
}
