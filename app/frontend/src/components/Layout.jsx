import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import {
  ArrowUpTrayIcon,
  ChatBubbleLeftRightIcon,
  ChartBarIcon,
  BookOpenIcon,
  ShareIcon,
  ClipboardDocumentCheckIcon,
  BeakerIcon,
  AcademicCapIcon,
  Cog6ToothIcon,
  PlusIcon,
  ClipboardDocumentListIcon,
  BoltIcon,
  ExclamationTriangleIcon,
  UserGroupIcon,
  StopCircleIcon,
  ServerIcon,
} from '@heroicons/react/24/outline'
import { getStats, createKB, listKBs, startLogCapture, stopLogCapture, getLogCaptureStatus, getVMStatus, startVM, stopVM } from '../api'
import { useAppState } from '../AppStateContext'

const primaryNav = [
  { to: '/chat',   label: 'Chat',   icon: ChatBubbleLeftRightIcon },
  { to: '/wiki',   label: 'Wiki',   icon: BookOpenIcon },
  { to: '/learn',  label: 'Learn',  icon: AcademicCapIcon },
  { to: '/viva',   label: 'Viva',   icon: UserGroupIcon },
]

const toolsNav = [
  { to: '/ingest',           label: 'Ingest',   icon: ArrowUpTrayIcon },
  { to: '/clinical-assess',  label: 'Clinical', icon: BeakerIcon },
  { to: '/order-generator',  label: 'Orders',   icon: ClipboardDocumentListIcon },
  { to: '/activity',         label: 'Activity', icon: BoltIcon },
  { to: '/gap-intelligence', label: 'Gaps',     icon: ExclamationTriangleIcon },
  { to: '/graph',            label: 'Graph',    icon: ShareIcon },
]

export default function Layout() {
  const [todaySpend, setTodaySpend] = useState(null)
  const { activeKB, switchKB, kbList, setKbList } = useAppState()
  const navigate = useNavigate()
  const [capturing, setCapturing] = useState(false)
  const [captureLines, setCaptureLines] = useState(0)
  const [captureError, setCaptureError] = useState(null)
  const [vmStatus, setVmStatus] = useState('unknown')
  const [vmBusy, setVmBusy] = useState(false)
  const [vmError, setVmError] = useState(null)

  useEffect(() => {
    getStats().then(s => setTodaySpend(s.total_cost_usd)).catch(() => {})
  }, [])

  // Poll VM status every 8s while transitioning, every 30s when stable
  useEffect(() => {
    getVMStatus().then(d => setVmStatus(d.status)).catch(() => {})
    const transitioning = vmStatus === 'STAGING' || vmStatus === 'STOPPING'
    const id = setInterval(() => {
      getVMStatus().then(d => setVmStatus(d.status)).catch(() => {})
    }, transitioning ? 6000 : 30000)
    return () => clearInterval(id)
  }, [vmStatus])

  // Poll capture status while active so line count updates live
  useEffect(() => {
    if (!capturing) return
    const id = setInterval(() => {
      getLogCaptureStatus()
        .then(s => setCaptureLines(s.lines))
        .catch(() => {})
    }, 2000)
    return () => clearInterval(id)
  }, [capturing])

  async function toggleCapture() {
    setCaptureError(null)
    try {
      if (!capturing) {
        await startLogCapture()
        setCapturing(true)
        setCaptureLines(0)
      } else {
        const result = await stopLogCapture()
        setCapturing(false)
        // Trigger browser download via the download endpoint
        const a = document.createElement('a')
        a.href = `/api/logs/download/${result.filename}`
        a.download = result.filename
        a.click()
      }
    } catch (e) {
      setCaptureError(e.message)
    }
  }

  async function toggleVM() {
    setVmError(null)
    setVmBusy(true)
    try {
      if (vmStatus === 'RUNNING') {
        const r = await stopVM()
        setVmStatus(r.status)
      } else {
        const r = await startVM()
        setVmStatus(r.status)
      }
    } catch (e) {
      setVmError(e.message)
    } finally {
      setVmBusy(false)
    }
  }

  async function handleNewKB() {
    const name = window.prompt('New knowledge base name (e.g. "philosophy"):')
    if (!name) return
    try {
      await createKB(name.trim())
      const data = await listKBs()
      setKbList(data.kbs || kbList)
      switchKB(name.trim().toLowerCase().replace(/ /g, '-'))
    } catch (e) {
      alert(`Failed to create KB: ${e.message}`)
    }
  }

  return (
    <div className="flex h-screen w-full bg-ink-950">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0 flex flex-col border-r border-border bg-ink-900">
        {/* Logo */}
        <div className="px-6 py-6 border-b border-border">
          <span className="font-display text-xl font-semibold text-accent tracking-tight">
            llm·wiki
          </span>
        </div>

        {/* KB selector */}
        <div className="px-3 pt-3 pb-2 border-b border-border">
          <p className="text-[10px] uppercase tracking-widest text-muted mb-1.5 px-1">
            Knowledge Base
          </p>
          <div className="flex items-center gap-1">
            <select
              value={activeKB}
              onChange={e => switchKB(e.target.value)}
              className="flex-1 bg-ink-800 border border-border rounded px-2 py-1.5 text-xs text-white focus:outline-none focus:border-accent truncate"
            >
              {kbList.map(kb => (
                <option key={kb} value={kb}>{kb}</option>
              ))}
            </select>
            <button
              onClick={handleNewKB}
              title="New knowledge base"
              className="p-1.5 rounded text-muted hover:text-white hover:bg-ink-700 transition-colors"
            >
              <PlusIcon className="w-3.5 h-3.5" />
            </button>
            <button
              onClick={() => navigate('/kb-settings')}
              title="Edit KB prompt"
              className="p-1.5 rounded text-muted hover:text-white hover:bg-ink-700 transition-colors"
            >
              <Cog6ToothIcon className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 px-3 py-4 space-y-1">
          {primaryNav.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-body transition-all duration-100 ${
                  isActive
                    ? 'bg-accent/10 text-accent border-l-2 border-accent pl-[10px]'
                    : 'text-muted hover:text-white hover:bg-ink-700'
                }`
              }
            >
              <Icon className="w-4 h-4 flex-shrink-0" />
              {label}
            </NavLink>
          ))}
        </nav>

        {/* Tools strip */}
        <div className="px-4 py-3 border-t border-border">
          <div className="flex items-center flex-wrap gap-0.5">
            {toolsNav.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                title={label}
                className={({ isActive }) =>
                  `p-1.5 rounded-md transition-colors duration-100 ${
                    isActive
                      ? 'text-accent bg-accent/10'
                      : 'text-muted hover:text-white hover:bg-ink-700'
                  }`
                }
              >
                <Icon className="w-4 h-4" />
              </NavLink>
            ))}
          </div>
        </div>

        {/* MedGemma VM control */}
        <div className="px-4 py-3 border-t border-border">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <ServerIcon className="w-3.5 h-3.5 text-muted flex-shrink-0" />
              <span className="text-[10px] text-muted truncate">MedGemma</span>
            </div>
            <div className="flex items-center gap-2">
              {/* Status dot */}
              <span
                className={`w-2 h-2 rounded-full flex-shrink-0 ${
                  vmStatus === 'RUNNING'
                    ? 'bg-green-400'
                    : vmStatus === 'STAGING' || vmStatus === 'STOPPING'
                    ? 'bg-yellow-400 animate-pulse'
                    : 'bg-zinc-600'
                }`}
                title={vmStatus}
              />
              {/* Start / Stop button */}
              <button
                onClick={toggleVM}
                disabled={vmBusy || vmStatus === 'STAGING' || vmStatus === 'STOPPING'}
                title={vmStatus === 'RUNNING' ? 'Stop MedGemma VM' : 'Start MedGemma VM'}
                className={`px-2 py-0.5 rounded text-[10px] font-mono transition-colors disabled:opacity-40 ${
                  vmStatus === 'RUNNING'
                    ? 'text-red-400 bg-red-400/10 hover:bg-red-400/20'
                    : 'text-green-400 bg-green-400/10 hover:bg-green-400/20'
                }`}
              >
                {vmStatus === 'STAGING' ? 'starting…'
                  : vmStatus === 'STOPPING' ? 'stopping…'
                  : vmBusy ? '…'
                  : vmStatus === 'RUNNING' ? 'stop'
                  : 'start'}
              </button>
            </div>
          </div>
          {vmError && (
            <p className="text-[10px] text-red-400 mt-1 truncate" title={vmError}>{vmError}</p>
          )}
        </div>

        {/* Bottom: spend + log capture */}
        <div className="px-4 py-3 border-t border-border space-y-2">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-xs text-muted">Total spend</p>
              <p className="font-mono text-sm text-white">
                {todaySpend !== null ? `$${todaySpend.toFixed(4)}` : '—'}
              </p>
            </div>
            <button
              onClick={toggleCapture}
              title={capturing ? `Stop capture (${captureLines} lines) — saves to file` : 'Start log capture'}
              className={`flex items-center gap-1 px-2 py-1 rounded text-xs font-mono transition-colors ${
                capturing
                  ? 'text-red-400 bg-red-400/10 hover:bg-red-400/20'
                  : 'text-muted hover:text-white hover:bg-ink-700'
              }`}
            >
              {capturing ? (
                <>
                  <span className="w-2 h-2 rounded-full bg-red-400 animate-pulse flex-shrink-0" />
                  {captureLines}
                </>
              ) : (
                <>
                  <span className="w-2 h-2 rounded-full bg-muted/40 flex-shrink-0" />
                  REC
                </>
              )}
            </button>
          </div>
          {captureError && (
            <p className="text-[10px] text-red-400 truncate">{captureError}</p>
          )}
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto bg-ink-950">
        <Outlet />
      </main>
    </div>
  )
}
