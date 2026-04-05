import { Outlet, NavLink } from 'react-router-dom'
import { useEffect, useState } from 'react'
import {
  ArrowUpTrayIcon,
  ChatBubbleLeftRightIcon,
  ChartBarIcon,
} from '@heroicons/react/24/outline'
import { getStats } from '../api'

const navItems = [
  { to: '/ingest', label: 'Ingest', icon: ArrowUpTrayIcon },
  { to: '/chat', label: 'Chat', icon: ChatBubbleLeftRightIcon },
  { to: '/dashboard', label: 'Dashboard', icon: ChartBarIcon },
]

export default function Layout() {
  const [todaySpend, setTodaySpend] = useState(null)

  useEffect(() => {
    getStats().then(s => setTodaySpend(s.total_cost_usd)).catch(() => {})
  }, [])

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

        {/* Nav */}
        <nav className="flex-1 px-3 py-4 space-y-1">
          {navItems.map(({ to, label, icon: Icon }) => (
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

        {/* Bottom spend */}
        <div className="px-4 py-4 border-t border-border">
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
