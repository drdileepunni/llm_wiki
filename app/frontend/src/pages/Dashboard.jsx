import { useEffect, useState } from 'react'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid
} from 'recharts'
import { getStats, getLog, getTimeseries } from '../api'

function StatCard({ label, value, sub }) {
  return (
    <div className="p-5 bg-surface border border-border rounded-xl">
      <p className="text-xs text-muted uppercase tracking-wider font-mono mb-2">{label}</p>
      <p className="text-2xl font-display font-semibold text-white">{value}</p>
      {sub && <p className="text-xs text-muted mt-1">{sub}</p>}
    </div>
  )
}

const OperationBadge = ({ op }) => (
  <span className={`px-2 py-0.5 rounded text-xs font-mono ${
    op === 'ingest'
      ? 'bg-accent/20 text-accent'
      : 'bg-success/20 text-success'
  }`}>
    {op}
  </span>
)

const CustomTooltip = ({ active, payload, label }) => {
  if (active && payload?.length) {
    return (
      <div className="bg-ink-800 border border-border rounded-lg px-3 py-2 text-xs font-mono">
        <p className="text-muted">{label}</p>
        <p className="text-warning">${payload[0].value.toFixed(4)}</p>
      </div>
    )
  }
  return null
}

export default function Dashboard() {
  const [stats, setStats] = useState(null)
  const [log, setLog] = useState([])
  const [timeseries, setTimeseries] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([getStats(), getLog(), getTimeseries()])
      .then(([s, l, t]) => {
        setStats(s)
        setLog(l)
        setTimeseries(t)
      })
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-muted text-sm">Loading dashboard...</p>
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto px-8 py-8 space-y-8">
      {/* Header */}
      <div>
        <h1 className="font-display text-2xl font-semibold text-white">Dashboard</h1>
        <p className="text-sm text-muted mt-0.5">Token usage and cost tracking</p>
      </div>

      {/* Stats cards */}
      {stats && (
        <div className="grid grid-cols-4 gap-4">
          <StatCard
            label="Total Spend"
            value={`$${stats.total_cost_usd.toFixed(4)}`}
            sub={`${stats.total_operations} operations`}
          />
          <StatCard
            label="Ingests"
            value={stats.ingest_count}
            sub={`avg $${stats.avg_ingest_cost_usd.toFixed(4)} each`}
          />
          <StatCard
            label="Chats"
            value={stats.chat_count}
            sub={`avg $${stats.avg_chat_cost_usd.toFixed(4)} each`}
          />
          <StatCard
            label="Projection (100 sources)"
            value={`$${stats.projection_100_sources_usd.toFixed(2)}`}
            sub={`~$${stats.projection_monthly_chat_usd.toFixed(2)}/mo chat`}
          />
        </div>
      )}

      {/* Chart */}
      {timeseries.length > 0 && (
        <div className="p-6 bg-surface border border-border rounded-xl">
          <p className="text-xs font-mono text-accent uppercase tracking-wider mb-5">
            Cumulative Cost
          </p>
          <ResponsiveContainer width="100%" height={200}>
            <LineChart data={timeseries}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2a2a3a" />
              <XAxis
                dataKey="date"
                tick={{ fill: '#6b6b8a', fontSize: 11, fontFamily: 'JetBrains Mono' }}
                axisLine={{ stroke: '#2a2a3a' }}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: '#6b6b8a', fontSize: 11, fontFamily: 'JetBrains Mono' }}
                axisLine={{ stroke: '#2a2a3a' }}
                tickLine={false}
                tickFormatter={v => `$${v}`}
              />
              <Tooltip content={<CustomTooltip />} />
              <Line
                type="monotone"
                dataKey="cumulative_cost"
                stroke="#7c6af7"
                strokeWidth={2}
                dot={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Log table */}
      <div className="p-6 bg-surface border border-border rounded-xl">
        <p className="text-xs font-mono text-accent uppercase tracking-wider mb-5">
          Operation Log
        </p>
        {log.length === 0 ? (
          <p className="text-sm text-muted">No operations yet</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left">
                  {['Timestamp', 'Operation', 'Source', 'Tokens In', 'Tokens Out', 'Cost'].map(h => (
                    <th key={h} className="pb-3 pr-6 text-xs font-mono text-muted uppercase tracking-wider">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {log.map(row => (
                  <tr key={row.id} className="hover:bg-ink-800/50 transition-colors">
                    <td className="py-3 pr-6 font-mono text-xs text-muted whitespace-nowrap">
                      {new Date(row.timestamp).toLocaleString()}
                    </td>
                    <td className="py-3 pr-6">
                      <OperationBadge op={row.operation} />
                    </td>
                    <td className="py-3 pr-6 text-white/70 text-xs max-w-[200px] truncate">
                      {row.source_name}
                    </td>
                    <td className="py-3 pr-6 font-mono text-xs text-muted">
                      {row.input_tokens?.toLocaleString()}
                    </td>
                    <td className="py-3 pr-6 font-mono text-xs text-muted">
                      {row.output_tokens?.toLocaleString()}
                    </td>
                    <td className="py-3 font-mono text-xs text-warning">
                      ${row.cost_usd.toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
