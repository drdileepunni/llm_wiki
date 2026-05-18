import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getValidationCatalog,
  getValidationResults,
  getValidationAnalysis,
  labelValidationResult,
  deleteValidationResult,
  startValidationRun,
  getValidationRunStatus,
  startValidationServe,
  getValidationServeStatus,
  stopValidationServe,
} from '../api'
import { useAppState } from '../AppStateContext'
import { ChevronDownIcon, CheckIcon } from '@heroicons/react/24/outline'

function StarBar({ value, max = 5 }) {
  const pct = Math.round((value / max) * 100)
  const color = value >= 4 ? 'bg-green-400' : value >= 3 ? 'bg-yellow-400' : 'bg-red-400'
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-ink-700 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs font-mono text-white w-6 text-right">{value.toFixed(1)}</span>
    </div>
  )
}

function ArmCard({ label, stats, color }) {
  if (!stats) return (
    <div className="flex-1 bg-ink-800 border border-border rounded-lg p-4 space-y-1 opacity-50">
      <p className={`text-xs font-semibold ${color}`}>{label}</p>
      <p className="text-xs text-muted">No reviews yet</p>
    </div>
  )
  return (
    <div className="flex-1 bg-ink-800 border border-border rounded-lg p-4 space-y-3">
      <div className="flex items-center justify-between">
        <p className={`text-xs font-semibold ${color}`}>{label}</p>
        <span className="text-xs text-muted">{stats.n} scenario{stats.n !== 1 ? 's' : ''}</span>
      </div>
      <div className="space-y-1">
        <div className="flex justify-between text-[10px] text-muted">
          <span>Avg rating</span>
          <span>{stats.min}–{stats.max}</span>
        </div>
        <StarBar value={stats.avg} />
      </div>
      <p className={`text-2xl font-bold font-mono ${color}`}>{stats.avg} <span className="text-xs text-muted font-normal">/ 5</span></p>
    </div>
  )
}

function ResultRow({ r, onServe, onRefresh, draggable, onDragStart }) {
  const [editing, setEditing]     = useState(false)
  const [labelVal, setLabelVal]   = useState(r.label || '')
  const [saving, setSaving]       = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [deleting, setDeleting]   = useState(false)
  const inputRef = useRef(null)

  useEffect(() => { if (editing && inputRef.current) inputRef.current.focus() }, [editing])

  async function saveLabel() {
    setSaving(true)
    try {
      await labelValidationResult(r.filename, labelVal)
      onRefresh()
    } catch {}
    setSaving(false)
    setEditing(false)
  }

  async function handleDelete() {
    setDeleting(true)
    try {
      await deleteValidationResult(r.filename)
      onRefresh()
    } catch (e) {
      alert(`Delete failed: ${e.message}`)
    }
    setDeleting(false)
    setConfirming(false)
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter') saveLabel()
    if (e.key === 'Escape') { setLabelVal(r.label || ''); setEditing(false) }
  }

  const meta = [
    r.n_scenarios != null ? `${r.n_scenarios} scenario${r.n_scenarios !== 1 ? 's' : ''}` : null,
    r.n_turns     != null ? `${r.n_turns} turn${r.n_turns !== 1 ? 's' : ''}` : null,
    r.kb          || null,
    r.diagnosis && r.diagnosis !== 'all' ? r.diagnosis : null,
  ].filter(Boolean).join(' · ')

  const dateStr = r.generated_at
    ? new Date(r.generated_at).toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' })
    : ''

  return (
    <div
      className={`bg-ink-800 border border-border rounded px-4 py-3 space-y-1 ${draggable ? 'cursor-grab active:cursor-grabbing' : ''}`}
      draggable={!!draggable}
      onDragStart={onDragStart}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0 flex-1">
          {/* Label row — click to edit */}
          {editing ? (
            <div className="flex items-center gap-2">
              <input
                ref={inputRef}
                value={labelVal}
                onChange={e => setLabelVal(e.target.value)}
                onKeyDown={handleKeyDown}
                placeholder="e.g. Sepsis pilot run"
                className="flex-1 bg-ink-700 border border-accent/40 rounded px-2 py-1 text-sm text-white focus:outline-none focus:border-accent"
              />
              <button onClick={saveLabel} disabled={saving}
                className="text-xs text-accent font-mono hover:text-white disabled:opacity-40">
                {saving ? '…' : 'Save'}
              </button>
              <button onClick={() => { setLabelVal(r.label || ''); setEditing(false) }}
                className="text-xs text-muted hover:text-white font-mono">
                ✕
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-2 min-w-0">
              {labelVal ? (
                <span className="text-sm text-white font-medium truncate">{labelVal}</span>
              ) : (
                <span className="text-xs font-mono text-muted truncate">{r.filename}</span>
              )}
              <button onClick={() => setEditing(true)}
                className="text-[10px] text-muted/50 hover:text-accent flex-shrink-0 transition-colors">
                ✎
              </button>
            </div>
          )}

          {/* Metadata row */}
          <p className="text-[11px] text-muted mt-0.5">
            {labelVal && <span className="text-muted/50 font-mono mr-1">{r.filename} · </span>}
            {meta}
            {dateStr ? ` · ${dateStr}` : ''}
          </p>
        </div>

        <div className="flex items-center gap-2 flex-shrink-0">
          {confirming ? (
            <>
              <span className="text-xs text-red-400">Delete?</span>
              <button
                onClick={handleDelete}
                disabled={deleting}
                className="text-xs font-semibold text-red-400 border border-red-400/40 hover:bg-red-400/10 rounded px-2 py-1.5 transition-colors disabled:opacity-40"
              >
                {deleting ? '…' : 'Yes'}
              </button>
              <button
                onClick={() => setConfirming(false)}
                className="text-xs text-muted hover:text-white rounded px-2 py-1.5 transition-colors"
              >
                No
              </button>
            </>
          ) : (
            <button
              onClick={() => setConfirming(true)}
              className="text-xs text-muted/50 hover:text-red-400 transition-colors px-1"
              title="Delete result file"
            >
              🗑
            </button>
          )}
          <button
            onClick={() => onServe(r.filename)}
            className="text-xs font-semibold text-accent border border-accent/40 hover:bg-accent/10 rounded px-3 py-1.5 transition-colors"
          >
            Serve &amp; Review
          </button>
        </div>
      </div>
    </div>
  )
}

function StatusBadge({ status }) {
  const cfg = {
    starting: { dot: 'bg-yellow-400 animate-pulse', text: 'text-yellow-400', label: 'Starting…' },
    running:  { dot: 'bg-accent animate-pulse',     text: 'text-accent',     label: 'Running'   },
    complete: { dot: 'bg-green-400',                text: 'text-green-400',  label: 'Complete'  },
    failed:   { dot: 'bg-red-400',                  text: 'text-red-400',    label: 'Failed'    },
  }
  const c = cfg[status] || { dot: 'bg-zinc-600', text: 'text-muted', label: status }
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-mono ${c.text}`}>
      <span className={`w-2 h-2 rounded-full flex-shrink-0 ${c.dot}`} />
      {c.label}
    </span>
  )
}

// Multi-select diagnosis dropdown — same pattern as Viva Batch
function DiagnosisSelect({ groups, selected, onChange }) {
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  // Close on outside click
  useEffect(() => {
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function toggle(id) {
    if (id === '__all__') {
      onChange([])
      return
    }
    const next = selected.includes(id)
      ? selected.filter(x => x !== id)
      : [...selected, id]
    onChange(next)
  }

  const isAll = selected.length === 0
  const label = isAll
    ? 'All (weighted mix)'
    : selected.length === 1
      ? groups.find(g => g.id === selected[0])?.label || selected[0]
      : `${selected.length} diagnoses selected`

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center justify-between bg-ink-800 border border-border rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-accent"
      >
        <span className="truncate">{label}</span>
        <ChevronDownIcon className={`w-4 h-4 text-muted flex-shrink-0 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute z-50 mt-1 w-full bg-ink-800 border border-border rounded shadow-xl max-h-72 overflow-y-auto">
          {/* All option */}
          <button
            type="button"
            onClick={() => toggle('__all__')}
            className={`w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-ink-700 transition-colors ${isAll ? 'text-accent' : 'text-muted'}`}
          >
            {isAll
              ? <CheckIcon className="w-3.5 h-3.5 flex-shrink-0" />
              : <span className="w-3.5 h-3.5 flex-shrink-0" />
            }
            All (weighted mix)
          </button>
          <div className="border-t border-border" />
          {groups.map(g => {
            const checked = selected.includes(g.id)
            return (
              <button
                key={g.id}
                type="button"
                onClick={() => toggle(g.id)}
                className={`w-full flex items-center gap-2 px-3 py-2 text-sm hover:bg-ink-700 transition-colors ${checked ? 'text-white' : 'text-muted'}`}
              >
                {checked
                  ? <CheckIcon className="w-3.5 h-3.5 text-accent flex-shrink-0" />
                  : <span className="w-3.5 h-3.5 flex-shrink-0" />
                }
                {g.label}
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

const STORAGE_KEY = 'validation_result_groups'

// groups: { id: string, name: string, filenames: string[] }[]
function useResultGroups() {
  const [groups, setGroups] = useState(() => {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || [] } catch { return [] }
  })

  function persist(updater) {
    setGroups(prev => {
      const next = updater(prev)
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
      return next
    })
  }

  const createGroup = useCallback(name => {
    persist(prev => [...prev, { id: crypto.randomUUID(), name, filenames: [] }])
  }, [])

  const renameGroup = useCallback((id, name) => {
    persist(prev => prev.map(g => g.id === id ? { ...g, name } : g))
  }, [])

  const deleteGroup = useCallback(id => {
    persist(prev => prev.filter(g => g.id !== id))
  }, [])

  const moveToGroup = useCallback((filename, groupId) => {
    persist(prev => prev.map(g => ({
      ...g,
      filenames: g.id === groupId
        ? g.filenames.includes(filename) ? g.filenames : [...g.filenames, filename]
        : g.filenames.filter(f => f !== filename),
    })))
  }, [])

  const removeFromGroup = useCallback((filename, groupId) => {
    persist(prev => prev.map(g => g.id === groupId
      ? { ...g, filenames: g.filenames.filter(f => f !== filename) }
      : g))
  }, [])

  return { groups, createGroup, renameGroup, deleteGroup, moveToGroup, removeFromGroup }
}

function DropZone({ onDrop, active, children, className = '' }) {
  const [over, setOver] = useState(false)
  return (
    <div
      className={`transition-colors rounded ${over && active ? 'ring-1 ring-accent/60 bg-accent/5' : ''} ${className}`}
      onDragOver={e => { if (active) { e.preventDefault(); setOver(true) } }}
      onDragLeave={() => setOver(false)}
      onDrop={e => { e.preventDefault(); setOver(false); onDrop(e) }}
    >
      {children}
    </div>
  )
}

function GroupPanel({ group, results, onServe, onRefresh, onRename, onDelete, onDrop, onRemove, draggingFile, makeDragStart }) {
  const [editing, setEditing] = useState(false)
  const [nameVal, setNameVal] = useState(group.name)
  const [collapsed, setCollapsed] = useState(false)
  const inputRef = useRef(null)

  useEffect(() => { if (editing && inputRef.current) inputRef.current.focus() }, [editing])

  function commitRename() {
    if (nameVal.trim()) onRename(group.id, nameVal.trim())
    setEditing(false)
  }

  const items = results.filter(r => group.filenames.includes(r.filename))

  return (
    <DropZone active={!!draggingFile} onDrop={() => draggingFile && onDrop(draggingFile, group.id)} className="border border-border rounded-lg overflow-hidden">
      {/* Group header */}
      <div className="flex items-center gap-2 px-3 py-2 bg-ink-800">
        <button type="button" onClick={() => setCollapsed(v => !v)} className="text-muted hover:text-white transition-colors">
          <ChevronDownIcon className={`w-3.5 h-3.5 transition-transform ${collapsed ? '-rotate-90' : ''}`} />
        </button>

        {editing ? (
          <input
            ref={inputRef}
            value={nameVal}
            onChange={e => setNameVal(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') commitRename(); if (e.key === 'Escape') { setNameVal(group.name); setEditing(false) } }}
            onBlur={commitRename}
            className="flex-1 bg-ink-700 border border-accent/40 rounded px-2 py-0.5 text-sm text-white focus:outline-none"
          />
        ) : (
          <button type="button" onClick={() => setEditing(true)} className="flex-1 text-left text-sm font-semibold text-white hover:text-accent transition-colors truncate">
            {group.name}
          </button>
        )}

        <span className="text-xs text-muted flex-shrink-0">{items.length}</span>
        <button
          type="button"
          onClick={() => { if (window.confirm(`Delete group "${group.name}"? Results will become ungrouped.`)) onDelete(group.id) }}
          className="text-muted/40 hover:text-red-400 transition-colors text-xs flex-shrink-0"
          title="Delete group"
        >✕</button>
      </div>

      {/* Group body */}
      {!collapsed && (
        <div className="p-2 space-y-2 min-h-[40px]">
          {items.length === 0 ? (
            <p className="text-xs text-muted/50 text-center py-2">Drop results here</p>
          ) : (
            items.map(r => (
              <div key={r.filename} className="relative group/row">
                <ResultRow r={r} onServe={onServe} onRefresh={onRefresh} draggable
                  onDragStart={makeDragStart(r.filename)}
                />
                <button
                  type="button"
                  onClick={() => onRemove(r.filename, group.id)}
                  className="absolute -right-1 -top-1 hidden group-hover/row:flex w-4 h-4 items-center justify-center bg-ink-700 border border-border rounded-full text-[9px] text-muted hover:text-white z-10"
                  title="Remove from group"
                >✕</button>
              </div>
            ))
          )}
        </div>
      )}
    </DropZone>
  )
}

function GroupedResults({ results, onServe, onRefresh }) {
  const { groups, createGroup, renameGroup, deleteGroup, moveToGroup, removeFromGroup } = useResultGroups()
  const [draggingFile, setDraggingFile] = useState(null)
  const [newGroupName, setNewGroupName] = useState('')
  const [creatingGroup, setCreatingGroup] = useState(false)
  const newGroupRef = useRef(null)

  useEffect(() => { if (creatingGroup && newGroupRef.current) newGroupRef.current.focus() }, [creatingGroup])

  const groupedFilenames = new Set(groups.flatMap(g => g.filenames))
  const ungrouped = results.filter(r => !groupedFilenames.has(r.filename))

  function handleCreateGroup() {
    const name = newGroupName.trim()
    if (!name) return
    createGroup(name)
    setNewGroupName('')
    setCreatingGroup(false)
  }

  function makeDragStart(filename) {
    return e => {
      e.dataTransfer.setData('text/plain', filename)
      e.dataTransfer.effectAllowed = 'move'
      setDraggingFile(filename)
    }
  }

  return (
    <div className="space-y-3" onDragEnd={() => setDraggingFile(null)}>
      {/* Named groups */}
      {groups.map(g => (
        <GroupPanel
          key={g.id}
          group={g}
          results={results}
          onServe={onServe}
          onRefresh={onRefresh}
          makeDragStart={makeDragStart}
          onRename={renameGroup}
          onDelete={deleteGroup}
          onDrop={moveToGroup}
          onRemove={removeFromGroup}
          draggingFile={draggingFile}
        />
      ))}

      {/* Create group row */}
      <div className="flex items-center gap-2">
        {creatingGroup ? (
          <>
            <input
              ref={newGroupRef}
              value={newGroupName}
              onChange={e => setNewGroupName(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleCreateGroup(); if (e.key === 'Escape') { setCreatingGroup(false); setNewGroupName('') } }}
              placeholder="Group name…"
              className="flex-1 bg-ink-800 border border-accent/40 rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-accent"
            />
            <button onClick={handleCreateGroup} className="text-xs text-accent font-mono hover:text-white px-2">Create</button>
            <button onClick={() => { setCreatingGroup(false); setNewGroupName('') }} className="text-xs text-muted hover:text-white font-mono px-1">✕</button>
          </>
        ) : (
          <button
            type="button"
            onClick={() => setCreatingGroup(true)}
            className="text-xs text-muted hover:text-accent transition-colors font-mono"
          >
            + New group
          </button>
        )}
      </div>

      {/* Ungrouped */}
      {ungrouped.length > 0 && (
        <DropZone
          active={!!draggingFile}
          onDrop={() => draggingFile && groups.length > 0 && null}
          className="border border-border/50 rounded-lg overflow-hidden"
        >
          <div className="px-3 py-2 bg-ink-800/50 flex items-center justify-between">
            <span className="text-xs text-muted font-semibold uppercase tracking-wider">Ungrouped</span>
            <span className="text-xs text-muted">{ungrouped.length}</span>
          </div>
          <div className="p-2 space-y-2">
            {ungrouped.map(r => (
              <ResultRow
                key={r.filename}
                r={r}
                onServe={onServe}
                onRefresh={onRefresh}
                draggable
                onDragStart={makeDragStart(r.filename)}
              />
            ))}
          </div>
        </DropZone>
      )}
    </div>
  )
}

export default function Validation() {
  const { activeKB } = useAppState()

  // ── Catalog ──────────────────────────────────────────────────────────────
  const [diagGroups, setDiagGroups] = useState([])
  useEffect(() => {
    getValidationCatalog()
      .then(d => setDiagGroups(d.groups || []))
      .catch(() => {})
  }, [])

  // ── Create Run form state ────────────────────────────────────────────────
  const [kb, setKb]                       = useState(activeKB || 'agent_school')
  const [diagnosisIds, setDiagnosisIds]   = useState([])          // [] = all
  const [mode, setMode]                   = useState('weighted')
  const [nScenarios, setNScenarios]       = useState(3)
  const [maxTurns, setMaxTurns]           = useState(4)
  const [seed, setSeed]                   = useState(42)
  const [skipArmB, setSkipArmB]           = useState(false)
  const [skipOrderGen, setSkipOrderGen]   = useState(false)
  const [launching, setLaunching]         = useState(false)
  const [launchError, setLaunchError]     = useState(null)

  useEffect(() => { if (activeKB) setKb(activeKB) }, [activeKB])

  // ── Active run ───────────────────────────────────────────────────────────
  const [runId, setRunId]       = useState(null)
  const [runState, setRunState] = useState(null)
  const logRef                  = useRef(null)

  useEffect(() => {
    if (!runId) return
    const active = runState?.status === 'starting' || runState?.status === 'running'
    if (!active && runState) return
    const id = setInterval(async () => {
      try { setRunState(await getValidationRunStatus(runId)) } catch {}
    }, 3000)
    return () => clearInterval(id)
  }, [runId, runState?.status])

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [runState?.log_tail])

  async function handleStartRun() {
    setLaunchError(null)
    setLaunching(true)
    setRunState(null)
    try {
      const { run_id } = await startValidationRun({
        n_scenarios:   nScenarios,
        seed,
        skip_arm_b:    skipArmB,
        skip_order_gen: skipOrderGen,
        kb,
        diagnosis_ids: diagnosisIds,
        mode,
        max_turns:     maxTurns,
      })
      setRunId(run_id)
      setRunState({ status: 'starting', log_tail: [], elapsed_s: 0 })
    } catch (e) {
      setLaunchError(e.message)
    } finally {
      setLaunching(false)
    }
  }

  // ── Analysis ─────────────────────────────────────────────────────────────
  const [analysis, setAnalysis]         = useState(null)

  async function fetchAnalysis() {
    try { setAnalysis(await getValidationAnalysis()) } catch {}
  }

  // ── Results list ─────────────────────────────────────────────────────────
  const [results, setResults]           = useState([])
  const [resultsError, setResultsError] = useState(null)
  const [serving, setServing]           = useState({ running: false, ngrok_url: null, local_url: null })
  const [serveTarget, setServeTarget]   = useState(null)
  const [serveError, setServeError]     = useState(null)

  async function fetchResults() {
    try {
      const d = await getValidationResults()
      setResults(d.results || [])
      setResultsError(null)
    } catch (e) { setResultsError(e.message) }
  }

  async function fetchServeStatus() {
    try {
      const s = await getValidationServeStatus()
      setServing(prev => ({
        running:   s.running,
        ngrok_url: s.ngrok_url,
        local_url: s.local_url || prev.local_url,
      }))
    } catch {}
  }

  useEffect(() => {
    fetchResults()
    fetchServeStatus()
    fetchAnalysis()
    const id = setInterval(() => { fetchResults(); fetchServeStatus() }, 10000)
    return () => clearInterval(id)
  }, [])

  useEffect(() => {
    if (runState?.status === 'complete') fetchResults()
  }, [runState?.status])

  async function handleServe(filename) {
    setServeError(null)
    setServeTarget(filename)
    try {
      const r = await startValidationServe({ filename })
      if (!r.serving) {
        setServeError('Server failed to start — check that the venv is set up correctly.')
        return
      }
      setServing({ running: r.serving, ngrok_url: r.ngrok_url, local_url: r.local_url })
    } catch (e) { setServeError(e.message) }
  }

  async function handleStopServe() {
    setServeError(null)
    try {
      await stopValidationServe()
      setServing({ running: false, ngrok_url: null, local_url: null })
      setServeTarget(null)
    } catch (e) { setServeError(e.message) }
  }

  const runIsActive = runState?.status === 'starting' || runState?.status === 'running'

  return (
    <div className="h-full overflow-auto bg-ink-950 p-6 space-y-6">
      <div className="max-w-4xl mx-auto space-y-6">

        {/* Header */}
        <div>
          <h1 className="text-xl font-semibold text-white">Validation</h1>
          <p className="text-sm text-muted mt-1">Run A/B tests comparing wiki-grounded vs MedGemma-only CDS.</p>
        </div>

        {/* ── Section 1: Create Run ────────────────────────────────────────── */}
        <div className="bg-ink-900 border border-border rounded-lg p-5 space-y-4">
          <h2 className="text-sm font-semibold text-white">New Run</h2>

          <div className="grid grid-cols-2 gap-4">

            {/* Mode */}
            <div className="space-y-1">
              <label className="text-xs text-muted">Mode</label>
              <select
                value={mode}
                onChange={e => setMode(e.target.value)}
                className="w-full bg-ink-800 border border-border rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-accent"
              >
                <option value="weighted">Weighted (by ICU frequency)</option>
                <option value="random">Random (uniform)</option>
              </select>
              <p className="text-[10px] text-muted/60">
                {mode === 'weighted'
                  ? 'Sample proportional to ICU admission frequency'
                  : 'Sample uniformly across all diagnosis × complication pairs'}
              </p>
            </div>

            {/* Diagnosis */}
            <div className="space-y-1">
              <label className="text-xs text-muted">Diagnosis</label>
              <DiagnosisSelect
                groups={diagGroups}
                selected={diagnosisIds}
                onChange={setDiagnosisIds}
              />
            </div>

            {/* KB */}
            <div className="space-y-1">
              <label className="text-xs text-muted">Knowledge Base</label>
              <input
                type="text"
                value={kb}
                onChange={e => setKb(e.target.value)}
                className="w-full bg-ink-800 border border-border rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-accent"
              />
            </div>

            {/* Seed */}
            <div className="space-y-1">
              <label className="text-xs text-muted">Seed</label>
              <input
                type="number"
                value={seed}
                onChange={e => setSeed(parseInt(e.target.value, 10))}
                className="w-full bg-ink-800 border border-border rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-accent"
              />
            </div>

            {/* N scenarios */}
            <div className="space-y-1">
              <label className="text-xs text-muted">Scenarios (1–20)</label>
              <input
                type="number" min={1} max={20}
                value={nScenarios}
                onChange={e => setNScenarios(parseInt(e.target.value, 10))}
                className="w-full bg-ink-800 border border-border rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-accent"
              />
            </div>

            {/* Max turns */}
            <div className="space-y-1">
              <label className="text-xs text-muted">Max turns per scenario</label>
              <input
                type="number" min={1} max={8}
                value={maxTurns}
                onChange={e => setMaxTurns(parseInt(e.target.value, 10))}
                className="w-full bg-ink-800 border border-border rounded px-3 py-1.5 text-sm text-white focus:outline-none focus:border-accent"
              />
            </div>

            {/* Toggles */}
            <div className="space-y-2 pt-5 col-span-2">
              <label className="flex items-center gap-2 cursor-pointer select-none">
                <input type="checkbox" checked={skipArmB}
                  onChange={e => setSkipArmB(e.target.checked)}
                  className="accent-accent" />
                <span className="text-xs text-muted">Skip Arm B (MedGemma) — Arm A only</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer select-none">
                <input type="checkbox" checked={skipOrderGen}
                  onChange={e => setSkipOrderGen(e.target.checked)}
                  className="accent-accent" />
                <span className="text-xs text-muted">Skip order structuring (faster, no structured orders)</span>
              </label>
            </div>
          </div>

          {launchError && <p className="text-xs text-red-400">{launchError}</p>}

          <button
            onClick={handleStartRun}
            disabled={launching || runIsActive}
            className="px-4 py-2 rounded bg-accent text-ink-950 text-sm font-semibold hover:bg-accent/90 disabled:opacity-40 transition-colors"
          >
            {launching ? 'Starting…' : 'Start Run'}
          </button>
        </div>

        {/* ── Section 2: Active Run Progress ───────────────────────────────── */}
        {runState && (
          <div className="bg-ink-900 border border-border rounded-lg p-5 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-white">Active Run</h2>
              <div className="flex items-center gap-3">
                <StatusBadge status={runState.status} />
                <span className="text-xs text-muted font-mono">{runState.elapsed_s}s</span>
              </div>
            </div>
            {runId && <p className="text-xs text-muted font-mono">run_id: {runId}</p>}

            {/* Case progress bar */}
            {(runState.n_cases_total > 0) && (
              <div>
                <div className="flex justify-between text-xs text-muted mb-1">
                  <span>Cases complete</span>
                  <span className="font-mono">
                    {runState.n_cases_done} / {runState.n_cases_total}
                  </span>
                </div>
                <div className="h-1.5 bg-ink-800 rounded-full overflow-hidden">
                  <div
                    className="h-full bg-blue-400 rounded-full transition-all duration-500"
                    style={{ width: `${Math.min(100, (runState.n_cases_done / runState.n_cases_total) * 100)}%` }}
                  />
                </div>
              </div>
            )}

            <div
              ref={logRef}
              className="bg-ink-950 border border-border rounded p-3 h-48 overflow-y-auto font-mono text-xs text-muted leading-relaxed"
            >
              {(runState.log_tail || []).length === 0 ? (
                <span className="text-muted/50">Waiting for output…</span>
              ) : (
                (runState.log_tail || []).map((line, i) => (
                  <div key={i} className={line.startsWith('ERROR') ? 'text-red-400' : line.startsWith('✓') ? 'text-green-400' : ''}>{line || ' '}</div>
                ))
              )}
            </div>
            {runState.status === 'complete' && (
              <p className="text-xs text-green-400 font-mono">
                ✓ Complete {runState.output_file ? `— ${runState.output_file}` : ''}
              </p>
            )}
            {runState.status === 'failed' && (
              <p className="text-xs text-red-400 font-mono">✗ Failed — check log above</p>
            )}
            {runIsActive && <p className="text-xs text-muted/60">Polling every 3 s…</p>}
          </div>
        )}

        {/* ── Section 3: Results ──────────────────────────────────────────── */}
        <div className="bg-ink-900 border border-border rounded-lg p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white">Results</h2>
            <button onClick={fetchResults} className="text-xs text-muted hover:text-white transition-colors">
              Refresh
            </button>
          </div>

          {/* Serve status banner */}
          {serving.running && (
            <div className="bg-green-900/20 border border-green-700/40 rounded p-3 flex items-center justify-between gap-3">
              <div className="space-y-1">
                <p className="text-xs text-green-400 font-semibold">
                  Server running{serveTarget ? ` — ${serveTarget}` : ''}
                </p>
                <a
                  href={serving.local_url || 'http://localhost:8081/ab_reviewer.html'}
                  target="_blank" rel="noopener noreferrer"
                  className="text-xs text-accent underline font-mono block"
                >
                  {serving.local_url || 'http://localhost:8081/ab_reviewer.html'}
                </a>
                {serving.ngrok_url && (
                  <a
                    href={serving.ngrok_url}
                    target="_blank" rel="noopener noreferrer"
                    className="text-xs text-accent/70 underline font-mono block"
                  >
                    {serving.ngrok_url}
                  </a>
                )}
              </div>
              <button
                onClick={handleStopServe}
                className="text-xs text-red-400 hover:text-red-300 font-mono px-2 py-1 rounded bg-red-400/10 hover:bg-red-400/20 transition-colors flex-shrink-0"
              >
                Stop
              </button>
            </div>
          )}

          {serveError && <p className="text-xs text-red-400">{serveError}</p>}
          {resultsError && <p className="text-xs text-red-400">{resultsError}</p>}

          {results.length === 0 ? (
            <p className="text-sm text-muted">No result files found yet.</p>
          ) : (
            <GroupedResults results={results} onServe={handleServe} onRefresh={fetchResults} />
          )}

          <p className="text-xs text-muted">Results list refreshes every 10 s.</p>
        </div>

        {/* ── Section 4: Review Analysis ───────────────────────────────── */}
        <div className="bg-ink-900 border border-border rounded-lg p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white">Review Analysis</h2>
            <button onClick={fetchAnalysis} className="text-xs text-muted hover:text-white transition-colors">
              Refresh
            </button>
          </div>

          {!analysis || analysis.n_reviewers === 0 ? (
            <p className="text-sm text-muted">No reviews submitted yet. Complete a review session to see analysis.</p>
          ) : (
            <div className="space-y-5">

              {/* Arm comparison cards */}
              <div className="flex gap-3">
                <ArmCard
                  label="Arm A — Wiki-Grounded"
                  stats={analysis.arm_a}
                  color="text-accent"
                />
                <ArmCard
                  label="Arm B — MedGemma Only"
                  stats={analysis.arm_b}
                  color="text-purple-400"
                />
              </div>

              {/* Winner callout */}
              {analysis.arm_a && analysis.arm_b && (
                <div className={`rounded p-3 text-sm font-semibold text-center ${
                  analysis.arm_a.avg > analysis.arm_b.avg
                    ? 'bg-accent/10 text-accent'
                    : analysis.arm_b.avg > analysis.arm_a.avg
                      ? 'bg-purple-400/10 text-purple-400'
                      : 'bg-zinc-700/30 text-muted'
                }`}>
                  {analysis.arm_a.avg > analysis.arm_b.avg
                    ? `Arm A (wiki) preferred  +${(analysis.arm_a.avg - analysis.arm_b.avg).toFixed(2)} stars`
                    : analysis.arm_b.avg > analysis.arm_a.avg
                      ? `Arm B (MedGemma) preferred  +${(analysis.arm_b.avg - analysis.arm_a.avg).toFixed(2)} stars`
                      : 'Draw — both arms rated equally'}
                </div>
              )}

              {/* Reviewers */}
              <div className="space-y-1">
                <p className="text-[10px] uppercase tracking-widest text-muted">
                  Reviewers ({analysis.n_reviewers})
                </p>
                {analysis.reviewers.map(r => (
                  <div key={r.name} className="flex items-center justify-between text-xs bg-ink-800 rounded px-3 py-2">
                    <span className="text-white">{r.name}</span>
                    <span className="text-muted">{r.role}</span>
                    <span className="text-muted">{r.n_scenarios} scenarios</span>
                    <span className="font-mono text-accent">{r.arm_a_avg != null ? `A: ${r.arm_a_avg}` : '—'}</span>
                    <span className="font-mono text-purple-400">{r.arm_b_avg != null ? `B: ${r.arm_b_avg}` : '—'}</span>
                  </div>
                ))}
              </div>

              {/* Per-scenario detail */}
              <div className="space-y-1">
                <p className="text-[10px] uppercase tracking-widest text-muted">Per-scenario ratings</p>
                <div className="space-y-1 max-h-72 overflow-y-auto">
                  {analysis.scenarios.map((s, i) => (
                    <div key={i} className="bg-ink-800 border border-border rounded px-3 py-2 space-y-1">
                      <div className="flex items-center gap-2">
                        <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                          s.arm_shown === 'arm_a'
                            ? 'bg-accent/10 text-accent'
                            : 'bg-purple-400/10 text-purple-400'
                        }`}>
                          {s.arm_shown === 'arm_a' ? 'Arm A' : 'Arm B'}
                        </span>
                        <span className="text-xs text-muted font-mono truncate">{s.scenario_id}</span>
                        <span className="text-xs text-muted ml-auto">{s.reviewer}</span>
                      </div>
                      <div className="flex items-center gap-1">
                        {s.ratings.map((r, j) => (
                          <span
                            key={j}
                            className={`text-[10px] font-mono px-1 py-0.5 rounded ${
                              r >= 4 ? 'bg-green-400/10 text-green-400'
                              : r >= 3 ? 'bg-yellow-400/10 text-yellow-400'
                              : 'bg-red-400/10 text-red-400'
                            }`}
                          >
                            T{j + 1}: {r}★
                          </span>
                        ))}
                        <span className="ml-auto text-xs font-mono text-white">{s.avg_rating}★ avg</span>
                      </div>
                      {s.notes.map((n, j) => (
                        <p key={j} className="text-[10px] text-muted italic">"{n}"</p>
                      ))}
                    </div>
                  ))}
                </div>
              </div>

            </div>
          )}
        </div>

      </div>
    </div>
  )
}
