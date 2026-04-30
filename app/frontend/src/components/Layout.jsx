import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { useEffect, useState } from 'react'
import {
  ArrowUpTrayIcon,
  ChatBubbleLeftRightIcon,
  ChartBarIcon,
  BookOpenIcon,
  ClipboardDocumentCheckIcon,
  BeakerIcon,
  AcademicCapIcon,
  Cog6ToothIcon,
  PlusIcon,
  ClipboardDocumentListIcon,
  BoltIcon,
} from '@heroicons/react/24/outline'
import { getStats, createKB, listKBs } from '../api'
import { useAppState } from '../AppStateContext'

const primaryNav = [
  { to: '/ingest', label: 'Ingest', icon: ArrowUpTrayIcon },
  { to: '/chat',   label: 'Chat',   icon: ChatBubbleLeftRightIcon },
  { to: '/wiki',   label: 'Wiki',   icon: BookOpenIcon },
  { to: '/learn',  label: 'Learn',  icon: AcademicCapIcon },
]

const toolsNav = [
  { to: '/assess',          label: 'Assess',    icon: ClipboardDocumentCheckIcon },
  { to: '/clinical-assess', label: 'Clinical',  icon: BeakerIcon },
  { to: '/order-generator', label: 'Orders',    icon: ClipboardDocumentListIcon },
  { to: '/activity',        label: 'Activity',  icon: BoltIcon },
  { to: '/dashboard',       label: 'Dashboard', icon: ChartBarIcon },
]

export default function Layout() {
  const [todaySpend, setTodaySpend] = useState(null)
  const { activeKB, switchKB, kbList, setKbList } = useAppState()
  const navigate = useNavigate()

  useEffect(() => {
    getStats().then(s => setTodaySpend(s.total_cost_usd)).catch(() => {})
  }, [])

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
          <div className="flex items-center gap-1">
            {toolsNav.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                title={label}
                className={({ isActive }) =>
                  `p-2 rounded-md transition-colors duration-100 ${
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

        {/* Bottom spend */}
        <div className="px-4 py-3 border-t border-border">
          <p className="text-xs text-muted mb-1">Total spend</p>
          <p className="font-mono text-sm text-white">
            {todaySpend !== null ? `$${todaySpend.toFixed(4)}` : '—'}
          </p>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto bg-ink-950">
        <Outlet />
      </main>
    </div>
  )
}
