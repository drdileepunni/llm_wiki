import { useState, useRef } from 'react'
import { ArrowUpTrayIcon, DocumentTextIcon } from '@heroicons/react/24/outline'
import { ingestFile, ingestPubmed } from '../api'
import CostBadge from '../components/CostBadge'

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
                tab === t
                  ? 'bg-accent text-white'
                  : 'text-muted hover:text-white'
              }`}
            >
              {t === 'file' ? 'Upload File' : 'PubMed ID'}
            </button>
          ))}
        </div>

        {tab === 'file' ? (
          <div className="flex flex-col gap-4">
            {/* Drop zone */}
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
      <div className="w-1/2 p-8 flex flex-col gap-6">
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

            {/* Files written */}
            <div className="p-5 bg-surface border border-border rounded-xl">
              <p className="text-xs font-mono text-accent uppercase tracking-wider mb-3">
                Files Written ({result.files_written?.length || 0})
              </p>
              {result.files_written?.length === 0 && (
                <p className="text-xs text-muted italic">No files written — Claude may have refused. Check the summary above.</p>
              )}
              <div className="flex flex-wrap gap-2">
                {result.files_written?.map(f => (
                  <span key={f} className="px-2 py-1 bg-ink-800 border border-border rounded text-xs font-mono text-muted">
                    {f}
                  </span>
                ))}
              </div>
            </div>

            {/* Errors (if any) */}
            {result.errors?.length > 0 && (
              <div className="p-4 bg-red-950/40 border border-red-800/50 rounded-xl">
                <p className="text-xs font-mono text-red-400 uppercase tracking-wider mb-2">Parse Errors</p>
                {result.errors.map((e, i) => (
                  <p key={i} className="text-xs font-mono text-red-300">{e}</p>
                ))}
              </div>
            )}

            {/* Cost */}
            <CostBadge
              inputTokens={result.input_tokens}
              outputTokens={result.output_tokens}
              costUsd={result.cost_usd}
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
