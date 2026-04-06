import { useState, useRef } from 'react'
import { ArrowUpTrayIcon, DocumentTextIcon, ChevronDownIcon, ChevronRightIcon } from '@heroicons/react/24/outline'
import { ingestFile, ingestPubmed } from '../api'
import CostBadge from '../components/CostBadge'

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

// ── Main page ──────────────────────────────────────────────────────────────────

export default function Ingest() {
  const [tab, setTab] = useState('file')
  const [dragging, setDragging] = useState(false)
  const [file, setFile] = useState(null)
  const [pmid, setPmid] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const inputRef = useRef()

  const handleDrop = (e) => {
    e.preventDefault()
    setDragging(false)
    const f = e.dataTransfer.files[0]
    if (f) setFile(f)
  }

  const handleIngestFile = async () => {
    if (!file) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await ingestFile(file)
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleIngestPubmed = async () => {
    if (!pmid.trim()) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const data = await ingestPubmed(pmid.trim())
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="h-full flex">
      {/* Left: Input */}
      <div className="w-1/2 border-r border-border p-8 flex flex-col gap-6">
        <div>
          <h1 className="font-display text-2xl font-semibold text-white mb-1">Ingest Source</h1>
          <p className="text-sm text-muted">Add a document to the wiki. Claude will extract and file it.</p>
        </div>

        {/* Tab switcher */}
        <div className="flex gap-1 p-1 bg-ink-800 rounded-lg w-fit">
          {['file', 'pubmed'].map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-1.5 text-sm rounded-md transition-all ${
                tab === t ? 'bg-accent text-white' : 'text-muted hover:text-white'
              }`}
            >
              {t === 'file' ? 'Upload File' : 'PubMed ID'}
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
            <input
              type="text"
              value={pmid}
              onChange={e => setPmid(e.target.value)}
              placeholder="e.g. 34567890"
              className="px-4 py-2.5 bg-ink-800 border border-border rounded-lg text-white font-mono text-sm placeholder:text-muted focus:outline-none focus:border-accent transition-colors"
            />
            <button
              onClick={handleIngestPubmed}
              disabled={!pmid.trim() || loading}
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
          <div className="flex-1 flex flex-col items-center justify-center text-center">
            <div className="w-16 h-16 rounded-full bg-ink-800 flex items-center justify-center mb-4">
              <DocumentTextIcon className="w-7 h-7 text-muted" />
            </div>
            <p className="text-sm text-muted">Results will appear here after ingestion</p>
          </div>
        )}
      </div>
    </div>
  )
}
