import { useEffect, useRef, useState, useCallback } from 'react'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from 'recharts'
import * as d3 from 'd3'
import { getGraphData } from '../api'
import { useAppState } from '../AppStateContext'

const ENTITY_COLOR = '#7c6af7'   // accent
const CONCEPT_COLOR = '#f59e0b'  // warning/amber
const GAP_COLOR = '#f87171'      // red-400

function nodeColor(n) {
  if (n.persistent_gap) return GAP_COLOR
  return n.type === 'entities' ? ENTITY_COLOR : CONCEPT_COLOR
}

function nodeRadius(n) {
  return Math.sqrt(Math.max(n.query_count, 0) + 1) * 4 + 5
}

const BarTooltip = ({ active, payload }) => {
  if (!active || !payload?.length) return null
  const d = payload[0].payload
  return (
    <div className="bg-ink-800 border border-border rounded-lg px-3 py-2 text-xs font-mono space-y-0.5">
      <p className="text-white font-semibold">{d.label}</p>
      <p className="text-muted">{d.type === 'entities' ? 'entity' : 'concept'}</p>
      <p style={{ color: nodeColor(d) }}>{d.query_count} queries</p>
    </div>
  )
}

export default function Graph() {
  const { activeKB } = useAppState()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [tooltip, setTooltip] = useState(null)
  const [filter, setFilter] = useState('all')   // all | entities | concepts | gaps
  const svgRef = useRef(null)
  const containerRef = useRef(null)
  const simRef = useRef(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    getGraphData(activeKB)
      .then(setData)
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }, [activeKB])

  const buildGraph = useCallback(() => {
    if (!data || !svgRef.current || !containerRef.current) return

    const el = svgRef.current
    d3.select(el).selectAll('*').remove()
    if (simRef.current) simRef.current.stop()

    const W = containerRef.current.clientWidth || 900
    const H = 560

    const visibleNodeIds = new Set(
      data.nodes
        .filter(n => {
          if (filter === 'entities') return n.type === 'entities'
          if (filter === 'concepts') return n.type === 'concepts'
          if (filter === 'gaps') return n.persistent_gap
          return true
        })
        .map(n => n.id)
    )

    const nodes = data.nodes
      .filter(n => visibleNodeIds.has(n.id))
      .map(n => ({ ...n }))

    const nodeMap = new Map(nodes.map(n => [n.id, n]))

    const edges = data.edges
      .filter(e => nodeMap.has(e.source) && nodeMap.has(e.target))
      .map(e => ({ source: e.source, target: e.target }))

    const svg = d3.select(el)
      .attr('width', W)
      .attr('height', H)
      .style('background', '#0a0a0f')

    const g = svg.append('g')

    // Zoom
    svg.call(
      d3.zoom()
        .scaleExtent([0.2, 4])
        .on('zoom', e => g.attr('transform', e.transform))
    )

    // Edges
    const link = g.append('g')
      .selectAll('line')
      .data(edges)
      .join('line')
      .attr('stroke', '#2a2a3a')
      .attr('stroke-width', 1)
      .attr('stroke-opacity', 0.6)

    // Nodes
    const node = g.append('g')
      .selectAll('circle')
      .data(nodes)
      .join('circle')
      .attr('r', nodeRadius)
      .attr('fill', nodeColor)
      .attr('fill-opacity', 0.85)
      .attr('stroke', n => n.persistent_gap ? '#fca5a5' : '#1a1a24')
      .attr('stroke-width', n => n.persistent_gap ? 2 : 1)
      .style('cursor', 'pointer')
      .on('mouseover', (event, n) => {
        d3.select(event.currentTarget).attr('fill-opacity', 1).attr('stroke', '#fff').attr('stroke-width', 2)
        setTooltip({ x: event.clientX, y: event.clientY, node: n })
      })
      .on('mousemove', (event) => {
        setTooltip(t => t ? { ...t, x: event.clientX, y: event.clientY } : null)
      })
      .on('mouseout', (event, n) => {
        d3.select(event.currentTarget)
          .attr('fill-opacity', 0.85)
          .attr('stroke', n.persistent_gap ? '#fca5a5' : '#1a1a24')
          .attr('stroke-width', n.persistent_gap ? 2 : 1)
        setTooltip(null)
      })
      .call(
        d3.drag()
          .on('start', (event, n) => {
            if (!event.active) simRef.current.alphaTarget(0.3).restart()
            n.fx = n.x; n.fy = n.y
          })
          .on('drag', (event, n) => { n.fx = event.x; n.fy = event.y })
          .on('end', (event, n) => {
            if (!event.active) simRef.current.alphaTarget(0)
            n.fx = null; n.fy = null
          })
      )

    // Labels for high-query nodes
    const label = g.append('g')
      .selectAll('text')
      .data(nodes.filter(n => n.query_count >= 5))
      .join('text')
      .text(n => n.label.length > 20 ? n.label.slice(0, 18) + '…' : n.label)
      .attr('font-size', 9)
      .attr('fill', '#6b6b8a')
      .attr('text-anchor', 'middle')
      .attr('dy', n => nodeRadius(n) + 11)
      .style('pointer-events', 'none')
      .style('font-family', 'JetBrains Mono, monospace')

    const sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(edges).id(n => n.id).distance(80).strength(0.4))
      .force('charge', d3.forceManyBody().strength(-120))
      .force('center', d3.forceCenter(W / 2, H / 2))
      .force('collide', d3.forceCollide(n => nodeRadius(n) + 4))
      .on('tick', () => {
        link
          .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
          .attr('x2', d => d.target.x).attr('y2', d => d.target.y)
        node.attr('cx', d => d.x).attr('cy', d => d.y)
        label.attr('x', d => d.x).attr('y', d => d.y)
      })

    simRef.current = sim
    return () => sim.stop()
  }, [data, filter])

  useEffect(() => {
    const cleanup = buildGraph()
    return cleanup
  }, [buildGraph])

  // Rebuild on resize
  useEffect(() => {
    const obs = new ResizeObserver(() => buildGraph())
    if (containerRef.current) obs.observe(containerRef.current)
    return () => obs.disconnect()
  }, [buildGraph])

  const topPages = data
    ? [...data.nodes].sort((a, b) => b.query_count - a.query_count).slice(0, 20)
    : []

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-muted text-sm">Building graph…</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="h-full flex items-center justify-center">
        <p className="text-red-400 text-sm">Error: {error}</p>
      </div>
    )
  }

  const totalNodes = data?.nodes.length ?? 0
  const totalEdges = data?.edges.length ?? 0
  const totalQueries = data?.nodes.reduce((s, n) => s + n.query_count, 0) ?? 0

  return (
    <div className="h-full overflow-y-auto px-8 py-8 space-y-8">
      {/* Header */}
      <div>
        <h1 className="font-display text-2xl font-semibold text-white">Knowledge Graph</h1>
        <p className="text-sm text-muted mt-0.5">
          {totalNodes} pages · {totalEdges} links · {totalQueries} total queries
        </p>
      </div>

      {/* Top queried pages */}
      <section className="bg-surface border border-border rounded-xl p-5">
        <h2 className="text-xs text-muted uppercase tracking-wider font-mono mb-4">Most Queried Pages</h2>
        <ResponsiveContainer width="100%" height={320}>
          <BarChart data={topPages} layout="vertical" margin={{ left: 160, right: 20, top: 0, bottom: 0 }}>
            <XAxis type="number" tick={{ fill: '#6b6b8a', fontSize: 10, fontFamily: 'JetBrains Mono' }} />
            <YAxis
              type="category"
              dataKey="label"
              width={155}
              tick={{ fill: '#a0a0be', fontSize: 10, fontFamily: 'JetBrains Mono' }}
              tickFormatter={v => v.length > 24 ? v.slice(0, 22) + '…' : v}
            />
            <Tooltip content={<BarTooltip />} />
            <Bar dataKey="query_count" radius={[0, 3, 3, 0]} maxBarSize={16}>
              {topPages.map(n => (
                <Cell key={n.id} fill={nodeColor(n)} fillOpacity={0.8} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>

        {/* Legend */}
        <div className="flex items-center gap-5 mt-3 text-xs text-muted font-mono">
          <span className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ background: ENTITY_COLOR }} />
            Entity
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ background: CONCEPT_COLOR }} />
            Concept
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-2.5 h-2.5 rounded-full inline-block" style={{ background: GAP_COLOR }} />
            Persistent gap
          </span>
        </div>
      </section>

      {/* Force graph */}
      <section className="bg-surface border border-border rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xs text-muted uppercase tracking-wider font-mono">Link Graph</h2>
          <div className="flex items-center gap-1">
            {['all', 'entities', 'concepts', 'gaps'].map(f => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-2.5 py-1 rounded text-xs font-mono transition-colors ${
                  filter === f
                    ? 'bg-accent/20 text-accent'
                    : 'text-muted hover:text-white hover:bg-ink-700'
                }`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>
        <p className="text-[10px] text-muted font-mono mb-3">Scroll to zoom · drag nodes · node size = query count</p>
        <div ref={containerRef} className="w-full rounded-lg overflow-hidden">
          <svg ref={svgRef} className="w-full" />
        </div>
      </section>

      {/* Hover tooltip rendered outside SVG */}
      {tooltip && (
        <div
          className="fixed z-50 pointer-events-none bg-ink-800 border border-border rounded-lg px-3 py-2 text-xs font-mono space-y-0.5 shadow-xl"
          style={{ left: tooltip.x + 14, top: tooltip.y - 10 }}
        >
          <p className="text-white font-semibold">{tooltip.node.label}</p>
          <p className="text-muted">{tooltip.node.type === 'entities' ? 'entity' : 'concept'}</p>
          <p style={{ color: nodeColor(tooltip.node) }}>{tooltip.node.query_count} queries</p>
          {tooltip.node.gap_opens > 0 && (
            <p className="text-red-400">{tooltip.node.gap_opens} gap opens</p>
          )}
        </div>
      )}
    </div>
  )
}
