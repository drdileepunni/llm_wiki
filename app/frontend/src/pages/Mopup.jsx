import { useState, useEffect, useRef, useCallback } from 'react'
import {
  SparklesIcon,
  ArrowPathIcon,
  PlayIcon,
  CheckCircleIcon,
  ExclamationTriangleIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  AdjustmentsHorizontalIcon,
} from '@heroicons/react/24/outline'
import { getMopupQueue, startMopup, getMopupJobStatus, getWikiContamination, runDefrag, runScanContamination, runMigrateScope, markFalsePositive, setPageSubtype, inferPageSubtype } from '../api'
import { useAppState } from '../AppStateContext'

// ── Helpers ───────────────────────────────────────────────────────────────────

function ScoreBadge({ score }) {
  const [bg, text, border] =
    score >= 80 ? ['bg-red-900/40',    'text-red-300',    'border-red-700/50']    :
    score >= 40 ? ['bg-amber-900/30',  'text-amber-400',  'border-amber-700/40']  :
    score >= 20 ? ['bg-accent/10',     'text-accent/80',  'border-accent/30']     :
                  ['bg-ink-700',       'text-muted',      'border-border']
  return (
    <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-mono border ${bg} ${text} ${border}`}>
      {score}
    </span>
  )
}

function WordsBadge({ words }) {
  const [bg, text, border] =
    words <= 20  ? ['bg-red-900/40',   'text-red-300',   'border-red-700/50']   :
    words <= 100 ? ['bg-amber-900/30', 'text-amber-400', 'border-amber-700/40'] :
                   ['bg-ink-700',      'text-muted',     'border-border']
  return (
    <span className={`inline-flex px-1.5 py-0.5 rounded text-[10px] font-mono border ${bg} ${text} ${border}`}>
      {words}w
    </span>
  )
}

function SubtypePill({ subtype }) {
  const colors = {
    medication: 'bg-blue-900/30 text-blue-300 border-blue-700/40',
    parameter:  'bg-teal-900/30 text-teal-300 border-teal-700/40',
    condition:  'bg-purple-900/30 text-purple-300 border-purple-700/40',
    default:    'bg-ink-700 text-muted border-border',
  }
  return (
    <span className={`inline-flex px-1.5 py-0.5 rounded text-[9px] font-mono border ${colors[subtype] || colors.default}`}>
      {subtype}
    </span>
  )
}

const SUBTYPES = ['medication', 'parameter', 'investigation', 'procedure', 'condition', 'default']

// ── Queue table ───────────────────────────────────────────────────────────────

function QueueTable({ pages, activeKB, onSubtypeChange }) {
  const [editing,   setEditing]   = useState(null)  // page rel being edited
  const [saving,    setSaving]    = useState(null)
  const [inferring, setInferring] = useState(null)

  if (!pages?.length) return (
    <div className="text-center py-16 text-muted text-sm">
      <CheckCircleIcon className="w-8 h-8 mx-auto mb-3 opacity-40" />
      No stubs above threshold — wiki is clean.
    </div>
  )

  async function handleSubtypeChange(pageRel, newSubtype) {
    setSaving(pageRel)
    try {
      await setPageSubtype(activeKB, pageRel, newSubtype)
      onSubtypeChange(pageRel, newSubtype)
    } catch (e) {
      alert('Failed to save: ' + e.message)
    } finally {
      setSaving(null)
      setEditing(null)
    }
  }

  async function handleInfer(pageRel) {
    setInferring(pageRel)
    try {
      const { subtype } = await inferPageSubtype(activeKB, pageRel)
      onSubtypeChange(pageRel, subtype)
    } catch (e) {
      alert('LLM inference failed: ' + e.message)
    } finally {
      setInferring(null)
    }
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border text-muted text-[10px] uppercase tracking-widest">
            <th className="text-left py-2 pr-4 font-medium">#</th>
            <th className="text-left py-2 pr-4 font-medium">Page</th>
            <th className="text-left py-2 pr-4 font-medium">Type</th>
            <th className="text-right py-2 pr-4 font-medium">Score</th>
            <th className="text-right py-2 pr-4 font-medium">CDS</th>
            <th className="text-right py-2 pr-4 font-medium">Inbound</th>
            <th className="text-right py-2 font-medium">Words</th>
          </tr>
        </thead>
        <tbody>
          {pages.map((p, i) => (
            <tr key={p.page} className="border-b border-border/50 hover:bg-ink-800/40 transition-colors">
              <td className="py-2 pr-4 text-muted font-mono">{i + 1}</td>
              <td className="py-2 pr-4">
                <span className="text-white font-medium">{p.title}</span>
                <span className="text-muted font-mono text-[9px] ml-2">{p.page}</span>
              </td>
              <td className="py-2 pr-4">
                {editing === p.page ? (
                  <select
                    autoFocus
                    disabled={saving === p.page}
                    defaultValue={p.subtype}
                    onChange={e => handleSubtypeChange(p.page, e.target.value)}
                    onBlur={() => setEditing(null)}
                    className="bg-ink-700 border border-accent/50 rounded px-1 py-0.5 text-[10px] font-mono text-white focus:outline-none"
                  >
                    {SUBTYPES.map(s => <option key={s} value={s}>{s}</option>)}
                  </select>
                ) : (
                  <button
                    onClick={() => setEditing(p.page)}
                    title="Click to change subtype"
                    className="group flex items-center gap-1"
                  >
                    <SubtypePill subtype={p.subtype} />
                    <span className="text-[8px] text-muted/0 group-hover:text-muted/60 transition-colors font-mono">✎</span>
                  </button>
                )}
                {p.subtype === 'default' && editing !== p.page && (
                  <button
                    onClick={() => handleInfer(p.page)}
                    disabled={!!inferring}
                    title="Ask MedGemma to classify this page's subtype"
                    className="ml-1.5 px-1 py-0.5 rounded text-[9px] font-mono border border-accent/30 text-accent/60 hover:text-accent hover:border-accent/60 transition-colors disabled:opacity-30"
                  >
                    {inferring === p.page ? '…' : '✦ AI'}
                  </button>
                )}
              </td>
              <td className="py-2 pr-4 text-right"><ScoreBadge score={p.score} /></td>
              <td className="py-2 pr-4 text-right font-mono text-muted">{p.cds}</td>
              <td className="py-2 pr-4 text-right font-mono text-muted">{p.inbound}</td>
              <td className="py-2 text-right"><WordsBadge words={p.words} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Run result card ───────────────────────────────────────────────────────────

function StubResultCard({ result }) {
  const [open, setOpen] = useState(false)
  const hasWork = result.sections_added?.length > 0
  const isError = !!result.error && !result.skipped

  return (
    <div className={`rounded-lg border p-3 space-y-1 ${
      isError   ? 'border-red-700/40 bg-red-950/10' :
      result.skipped ? 'border-border bg-ink-900 opacity-50' :
      hasWork   ? 'border-emerald-700/40 bg-emerald-950/10' :
                  'border-border bg-ink-900'
    }`}>
      <div
        className="flex items-center justify-between cursor-pointer"
        onClick={() => hasWork && setOpen(o => !o)}
      >
        <div className="flex items-center gap-2">
          {isError      ? <ExclamationTriangleIcon className="w-3.5 h-3.5 text-red-400 flex-shrink-0" /> :
           result.skipped ? <CheckCircleIcon className="w-3.5 h-3.5 text-muted flex-shrink-0" /> :
           hasWork       ? <CheckCircleIcon className="w-3.5 h-3.5 text-emerald-400 flex-shrink-0" /> :
                           <ExclamationTriangleIcon className="w-3.5 h-3.5 text-amber-400 flex-shrink-0" />}
          <span className="text-sm text-white font-medium">{result.page}</span>
        </div>
        <div className="flex items-center gap-2">
          {isError && <span className="text-[10px] text-red-400">{result.error}</span>}
          {result.skipped && <span className="text-[10px] text-muted">skipped</span>}
          {hasWork && (
            <>
              <span className="text-[10px] text-emerald-400 font-mono">
                +{result.sections_added.length} sections
              </span>
              {open
                ? <ChevronUpIcon className="w-3 h-3 text-muted" />
                : <ChevronDownIcon className="w-3 h-3 text-muted" />}
            </>
          )}
        </div>
      </div>
      {open && hasWork && (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {result.sections_added.map(s => (
            <span key={s} className="px-2 py-0.5 rounded text-[10px] bg-emerald-900/30 text-emerald-300 border border-emerald-700/40 font-mono">
              {s}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function RunResults({ result }) {
  const defrag = result.defrag || {}
  return (
    <div className="space-y-6">
      {/* Summary bar */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'Stubs expanded', value: result.stubs_expanded, color: 'text-emerald-400' },
          { label: 'Skipped',        value: result.stubs_skipped,  color: 'text-muted'       },
          { label: 'Errors',         value: result.stubs_errored,  color: result.stubs_errored > 0 ? 'text-red-400' : 'text-muted' },
          { label: 'Defrag moves',   value: defrag.total_moves ?? '—', color: 'text-accent'  },
        ].map(({ label, value, color }) => (
          <div key={label} className="rounded-lg border border-border bg-ink-900 p-3 text-center">
            <p className={`text-2xl font-bold font-mono ${color}`}>{value}</p>
            <p className="text-[10px] text-muted uppercase tracking-widest mt-1">{label}</p>
          </div>
        ))}
      </div>

      {/* Per-page results */}
      {result.stub_results?.length > 0 && (
        <div>
          <p className="text-[10px] text-muted uppercase tracking-widest mb-3">Page results</p>
          <div className="space-y-2">
            {result.stub_results.map(r => (
              <StubResultCard key={r.page} result={r} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Settings panel ────────────────────────────────────────────────────────────

function Settings({ params, onChange }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
      {[
        { key: 'scoreThreshold', label: 'Min score', min: 0,  max: 200, hint: 'Pages below this score are ignored' },
        { key: 'wordThreshold',  label: 'Stub ≤ words', min: 50, max: 800, hint: 'Pages with fewer words are treated as stubs' },
        { key: 'maxStubs',       label: 'Max stubs / run', min: 1, max: 100, hint: 'Safety cap on LLM calls per run' },
      ].map(({ key, label, min, max, hint }) => (
        <div key={key}>
          <label className="block text-[10px] text-muted uppercase tracking-widest mb-1">{label}</label>
          <input
            type="number"
            min={min}
            max={max}
            value={params[key]}
            onChange={e => onChange({ ...params, [key]: Number(e.target.value) })}
            className="w-full bg-ink-800 border border-border rounded px-3 py-1.5 text-sm text-white font-mono focus:outline-none focus:border-accent"
          />
          <p className="text-[9px] text-muted mt-1">{hint}</p>
        </div>
      ))}
      <div>
        <label className="block text-[10px] text-muted uppercase tracking-widest mb-1">Run defrag</label>
        <button
          onClick={() => onChange({ ...params, runDefrag: !params.runDefrag })}
          className={`w-full rounded px-3 py-1.5 text-sm font-medium border transition-colors ${
            params.runDefrag
              ? 'bg-accent/20 border-accent/50 text-accent'
              : 'bg-ink-800 border-border text-muted'
          }`}
        >
          {params.runDefrag ? 'Yes — fix contamination' : 'No — stubs only'}
        </button>
        <p className="text-[9px] text-muted mt-1">Defrag moves out-of-scope content to correct pages</p>
      </div>
    </div>
  )
}

// ── Scope Contamination tab ───────────────────────────────────────────────────

function ScopeTab({ activeKB }) {
  const [data, setData]     = useState(null)
  const [loading, setLoading] = useState(false)
  const [running, setRunning] = useState(null)
  const [toast, setToast]   = useState(null)

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
      load()
      showToast('Defrag started — check Activity feed for results')
    } catch (e) { showToast('Defrag failed: ' + e.message, false) }
    finally { setRunning(null) }
  }

  const handleDefragPage = async (path) => {
    setRunning('defrag-' + path)
    try {
      await runDefrag(activeKB, path)
      await new Promise(r => setTimeout(r, 2000))
      load()
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
      setTimeout(() => load(), 30000)
    } catch (e) { showToast('Scan failed: ' + e.message, false) }
    finally { setRunning(null) }
  }

  const handleMigrateScope = async () => {
    setRunning('scope')
    try {
      await runMigrateScope(activeKB)
      await new Promise(r => setTimeout(r, 3000))
      showToast('Scope fields added — check Activity feed for details')
    } catch (e) { showToast('Migration failed: ' + e.message, false) }
    finally { setRunning(null) }
  }

  const handleFalsePositive = async (path, section, belongs_on) => {
    try {
      await markFalsePositive(activeKB, path, section, belongs_on)
      load()
      showToast(`Whitelisted: "${section}" will not be flagged again`)
    } catch (e) { showToast('Failed: ' + e.message, false) }
  }

  const pages = data?.pages ?? []

  return (
    <div className="max-w-3xl space-y-4">
      {/* Toast */}
      {toast && (
        <div className={`px-3 py-2 rounded-md text-xs font-mono border ${
          toast.ok ? 'bg-success/10 text-success border-success/30' : 'bg-red-950 text-red-400 border-red-800'
        }`}>
          {toast.msg}
        </div>
      )}

      {/* Actions */}
      <div className="flex flex-wrap gap-2 items-center">
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
        <button
          onClick={load}
          disabled={loading}
          className="ml-auto flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs
            bg-ink-800 text-muted border border-border hover:text-white transition-colors disabled:opacity-40"
        >
          <ArrowPathIcon className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {/* List */}
      {loading ? (
        <p className="text-xs text-muted py-8 text-center">Loading…</p>
      ) : pages.length === 0 ? (
        <div className="text-center py-16 text-muted text-sm">
          <CheckCircleIcon className="w-8 h-8 mx-auto mb-3 opacity-40" />
          No contamination detected. Run a scan to check existing pages.
        </div>
      ) : (
        <div className="space-y-3">
          {pages.map((page, i) => (
            <div key={i} className="rounded-lg border border-orange-800/40 bg-orange-950/20 p-3">
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-semibold text-white">{page.title}</p>
                  <p className="text-[10px] font-mono text-muted mt-0.5">{page.path}</p>
                  <div className="flex flex-wrap gap-1 mt-2">
                    {page.violations.map((v, j) => (
                      <span key={j} className="inline-flex items-center gap-1 text-[9px] pl-1.5 pr-0.5 py-0.5 rounded bg-orange-950 border border-orange-800 text-orange-400 font-mono">
                        {v.section} → {v.belongs_on}
                        <button
                          title="Mark as false positive"
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
          ))}
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

const TABS = [
  { id: 'queue',   label: 'Stub Queue'        },
  { id: 'run',     label: 'Run'               },
  { id: 'results', label: 'Last Result'       },
  { id: 'scope',   label: 'Scope Contamination' },
]

const DEFAULT_PARAMS = {
  scoreThreshold: 20,
  wordThreshold:  300,
  maxStubs:       50,
  runDefrag:      true,
}

export default function Mopup() {
  const { activeKB } = useAppState()
  const [tab, setTab]           = useState('queue')
  const [params, setParams]     = useState(DEFAULT_PARAMS)
  const [showSettings, setShowSettings] = useState(false)

  // Queue state
  const [queue, setQueue]           = useState(null)
  const [queueLoading, setQueueLoading] = useState(false)
  const [queueError, setQueueError] = useState(null)

  // Job state — jobResult is persisted in localStorage so it survives page refresh
  const [jobId, setJobId]         = useState(null)
  const [jobStatus, setJobStatus] = useState(null)  // 'running' | 'done' | 'error'
  const [jobResult, setJobResult] = useState(() => {
    try { return JSON.parse(localStorage.getItem('mopup_last_result') || 'null') }
    catch { return null }
  })
  const [jobError, setJobError]   = useState(null)
  const [launching, setLaunching] = useState(false)
  const pollRef = useRef(null)

  // Persist result to localStorage whenever it changes
  useEffect(() => {
    if (jobResult) localStorage.setItem('mopup_last_result', JSON.stringify(jobResult))
  }, [jobResult])

  // Load queue on mount / KB change
  useEffect(() => {
    loadQueue()
  }, [activeKB])

  // Poll job status
  useEffect(() => {
    if (!jobId || jobStatus === 'done' || jobStatus === 'error') return
    pollRef.current = setInterval(async () => {
      try {
        const data = await getMopupJobStatus(jobId, activeKB)
        setJobStatus(data.status)
        if (data.status === 'done') {
          setJobResult(data.result)
          clearInterval(pollRef.current)
        } else if (data.status === 'error') {
          setJobError(data.error)
          clearInterval(pollRef.current)
        }
      } catch (_) {}
    }, 3000)
    return () => clearInterval(pollRef.current)
  }, [jobId, jobStatus, activeKB])

  async function loadQueue() {
    setQueueLoading(true)
    setQueueError(null)
    try {
      const data = await getMopupQueue(activeKB, {
        scoreThreshold: params.scoreThreshold,
        wordThreshold:  params.wordThreshold,
        maxStubs:       params.maxStubs,
      })
      setQueue(data)
    } catch (e) {
      setQueueError(e.message)
    } finally {
      setQueueLoading(false)
    }
  }

  async function handleRun() {
    setLaunching(true)
    setJobId(null)
    setJobStatus(null)
    setJobResult(null)
    setJobError(null)
    try {
      const { job_id } = await startMopup(activeKB, params)
      setJobId(job_id)
      setJobStatus('running')
      setTab('run')
    } catch (e) {
      setJobError(e.message)
    } finally {
      setLaunching(false)
    }
  }

  const isRunning = jobStatus === 'running'

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Header */}
      <div className="flex-shrink-0 px-6 pt-6 pb-4 border-b border-border">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <SparklesIcon className="w-5 h-5 text-accent" />
              <h1 className="text-lg font-semibold text-white">Wiki Mop-up</h1>
            </div>
            <p className="text-xs text-muted">
              Find important stub pages and expand them via MedGemma. Then fix scope contamination.
            </p>
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            <button
              onClick={() => setShowSettings(s => !s)}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded border text-xs transition-colors ${
                showSettings
                  ? 'bg-accent/20 border-accent/50 text-accent'
                  : 'bg-ink-800 border-border text-muted hover:text-white'
              }`}
            >
              <AdjustmentsHorizontalIcon className="w-3.5 h-3.5" />
              Settings
            </button>
            <button
              onClick={loadQueue}
              disabled={queueLoading}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded border border-border bg-ink-800 text-xs text-muted hover:text-white transition-colors disabled:opacity-40"
            >
              <ArrowPathIcon className={`w-3.5 h-3.5 ${queueLoading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
            <button
              onClick={handleRun}
              disabled={isRunning || launching}
              className="flex items-center gap-1.5 px-4 py-1.5 rounded border border-accent/60 bg-accent/20 text-accent text-xs font-medium hover:bg-accent/30 transition-colors disabled:opacity-40"
            >
              {isRunning
                ? <ArrowPathIcon className="w-3.5 h-3.5 animate-spin" />
                : <PlayIcon className="w-3.5 h-3.5" />}
              {isRunning ? 'Running…' : 'Run Mop-up'}
            </button>
          </div>
        </div>

        {/* Settings panel */}
        {showSettings && (
          <div className="mt-4 p-4 rounded-lg border border-border bg-ink-900">
            <Settings params={params} onChange={p => { setParams(p); setQueue(null) }} />
          </div>
        )}

        {/* Tabs */}
        <div className="flex gap-1 mt-4">
          {TABS.map(t => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                tab === t.id
                  ? 'bg-accent/20 text-accent border border-accent/40'
                  : 'text-muted hover:text-white border border-transparent'
              }`}
            >
              {t.label}
              {t.id === 'queue' && queue && (
                <span className="ml-1.5 font-mono text-[9px] opacity-70">{queue.total}</span>
              )}
              {t.id === 'run' && isRunning && (
                <span className="ml-1.5 w-1.5 h-1.5 rounded-full bg-amber-400 inline-block animate-pulse" />
              )}
              {t.id === 'results' && jobResult && (
                <span className="ml-1.5 font-mono text-[9px] opacity-70 text-emerald-400">
                  {jobResult.stubs_expanded}↑
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-6 py-5">

        {/* Queue tab */}
        {tab === 'queue' && (
          <div className="max-w-5xl">
            {/* Metric summary */}
            {queue && (
              <div className="grid grid-cols-3 gap-3 mb-5">
                {[
                  { label: 'Stub pages in queue', value: queue.total },
                  { label: 'Score threshold',     value: `≥ ${queue.score_threshold}` },
                  { label: 'Word threshold',       value: `< ${queue.word_threshold}w` },
                ].map(({ label, value }) => (
                  <div key={label} className="rounded-lg border border-border bg-ink-900 p-3 text-center">
                    <p className="text-xl font-bold font-mono text-white">{value}</p>
                    <p className="text-[10px] text-muted uppercase tracking-widest mt-1">{label}</p>
                  </div>
                ))}
              </div>
            )}

{queueLoading && (
              <div className="flex items-center justify-center py-16 gap-2 text-muted text-sm">
                <ArrowPathIcon className="w-5 h-5 animate-spin" />
                Scoring pages…
              </div>
            )}
            {queueError && !queue && (
              <div className="rounded-lg border border-red-700/40 bg-red-950/10 p-4 text-sm text-red-400">
                {queueError}
              </div>
            )}
            {!queueLoading && queue && (
              <QueueTable
                pages={queue.pages}
                activeKB={activeKB}
                onSubtypeChange={(pageRel, newSubtype) =>
                  setQueue(q => ({
                    ...q,
                    pages: q.pages.map(p =>
                      p.page === pageRel
                        ? { ...p, subtype: newSubtype, subtype_inferred: false }
                        : p
                    ),
                  }))
                }
              />
            )}
          </div>
        )}

        {/* Run tab */}
        {tab === 'run' && (
          <div className="max-w-xl space-y-4">
            {!jobId && !jobError && (
              <div className="text-center py-16 text-muted text-sm">
                <SparklesIcon className="w-8 h-8 mx-auto mb-3 opacity-40" />
                Press <span className="text-accent">Run Mop-up</span> to start.
              </div>
            )}

            {jobId && (
              <div className={`rounded-lg border p-4 space-y-3 ${
                jobStatus === 'done'    ? 'border-emerald-700/40 bg-emerald-950/10' :
                jobStatus === 'error'   ? 'border-red-700/40 bg-red-950/10' :
                                          'border-amber-700/40 bg-amber-950/10'
              }`}>
                <div className="flex items-center gap-2">
                  {jobStatus === 'running' && <ArrowPathIcon className="w-4 h-4 text-amber-400 animate-spin" />}
                  {jobStatus === 'done'    && <CheckCircleIcon className="w-4 h-4 text-emerald-400" />}
                  {jobStatus === 'error'   && <ExclamationTriangleIcon className="w-4 h-4 text-red-400" />}
                  <span className="text-sm font-medium text-white">
                    Job <span className="font-mono text-accent">{jobId}</span>
                    {' — '}
                    {jobStatus === 'running' ? 'running…' :
                     jobStatus === 'done'    ? 'complete' : 'failed'}
                  </span>
                </div>

                {jobStatus === 'running' && (
                  <p className="text-xs text-muted">
                    Expanding stubs via MedGemma. This may take several minutes depending on queue size.
                  </p>
                )}

                {jobStatus === 'done' && (
                  <button
                    onClick={() => setTab('results')}
                    className="text-xs text-accent hover:underline"
                  >
                    View detailed results →
                  </button>
                )}

                {jobStatus === 'error' && (
                  <p className="text-xs text-red-400 font-mono">{jobError}</p>
                )}
              </div>
            )}

            {jobError && !jobId && (
              <div className="rounded-lg border border-red-700/40 bg-red-950/10 p-4 text-sm text-red-400">
                {jobError}
              </div>
            )}
          </div>
        )}

        {/* Results tab */}
        {tab === 'results' && (
          <div className="max-w-3xl">
            {!jobResult && (
              <div className="text-center py-16 text-muted text-sm">
                <SparklesIcon className="w-8 h-8 mx-auto mb-3 opacity-40" />
                No results yet — run a mop-up first.
              </div>
            )}
            {jobResult && <RunResults result={jobResult} />}
          </div>
        )}

        {/* Scope tab */}
        {tab === 'scope' && <ScopeTab activeKB={activeKB} />}
      </div>
    </div>
  )
}
