import { useEffect, useState, useCallback } from 'react'
import { getTraceRuns, getTraceRunPatients, getTraceEntry } from '../api'
import { ChevronDownIcon, ChevronRightIcon } from '@heroicons/react/24/outline'

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtUsd(v) {
  if (v == null) return '—'
  if (v < 0.0001) return '<$0.0001'
  return `$${Number(v).toFixed(4)}`
}

function fmtTokens(n) {
  if (n == null) return '—'
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}

function relTime(iso) {
  if (!iso) return '—'
  const diff = (Date.now() - new Date(iso)) / 1000
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

function parseVitals(text) {
  if (!text) return []
  return text.split('\n')
    .map(l => l.trim())
    .filter(l => l.startsWith('['))
    .map(l => {
      const tsMatch = l.match(/\[([^\]]+)\]/)
      const ts = tsMatch ? tsMatch[1] : ''
      const vals = l.replace(/\[[^\]]+\]\s*/, '')
      return { ts, vals }
    })
}

function parseLabs(text) {
  if (!text) return []
  return text.split('\n')
    .map(l => l.trim())
    .filter(l => l.startsWith('['))
    .map(l => {
      const tsMatch = l.match(/\[([^\]]+)\]/)
      const ts = tsMatch ? tsMatch[1] : ''
      const vals = l.replace(/\[[^\]]+\]\s*/, '')
      return { ts, vals }
    })
}

function parseLongitudinal(text) {
  if (!text) return null
  try {
    const jsonMatch = text.match(/(\{[\s\S]*\})/)
    if (jsonMatch) return JSON.parse(jsonMatch[1])
  } catch (_) {}
  return null
}

const STATUS_COLOR = {
  critical:  'text-red-400 bg-red-900/40',
  worsening: 'text-orange-400 bg-orange-900/40',
  stable:    'text-yellow-400 bg-yellow-900/40',
  improving: 'text-green-400 bg-green-900/40',
  resolved:  'text-gray-400 bg-gray-700/40',
}

// ── Left panel: run + patient list ────────────────────────────────────────────

function RunList({ selectedRunId, onSelectRun, onSelectPatient, selectedPatientId }) {
  const [runs, setRuns]         = useState([])
  const [loading, setLoading]   = useState(true)
  const [expanded, setExpanded] = useState({})  // date → bool
  const [patients, setPatients] = useState({})  // "date/hour/mode" → list
  const [pLoading, setPLoading] = useState({})

  useEffect(() => {
    getTraceRuns()
      .then(d => {
        setRuns(d.runs || [])
        // auto-expand most recent date
        if (d.runs?.length > 0) {
          setExpanded({ [d.runs[0].date]: true })
        }
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  // group runs by date
  const byDate = {}
  for (const r of runs) {
    if (!byDate[r.date]) byDate[r.date] = []
    byDate[r.date].push(r)
  }

  async function selectRun(run) {
    const key = `${run.date}/${run.hour}/${run.mode}`
    onSelectRun(key)
    if (patients[key]) return
    setPLoading(p => ({ ...p, [key]: true }))
    try {
      const d = await getTraceRunPatients(run.date, run.hour, run.mode)
      setPatients(p => ({ ...p, [key]: d.patients || [] }))
    } catch (e) {
      console.error(e)
    } finally {
      setPLoading(p => ({ ...p, [key]: false }))
    }
  }

  if (loading) return <div className="p-4 text-gray-500 text-sm">Loading runs…</div>

  return (
    <div className="flex flex-col h-full overflow-y-auto">
      <div className="px-4 py-3 border-b border-gray-700 flex-shrink-0">
        <span className="text-white font-semibold text-sm">Run Explorer</span>
      </div>
      <div className="flex-1 overflow-y-auto">
        {Object.keys(byDate).sort().reverse().map(date => (
          <div key={date}>
            {/* Date header */}
            <button
              onClick={() => setExpanded(e => ({ ...e, [date]: !e[date] }))}
              className="w-full flex items-center gap-2 px-4 py-2 text-left hover:bg-gray-800/50 transition-colors"
            >
              {expanded[date]
                ? <ChevronDownIcon className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
                : <ChevronRightIcon className="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
              }
              <span className="text-xs font-medium text-gray-300">{date}</span>
              <span className="ml-auto text-xs text-gray-500">
                {byDate[date].reduce((s, r) => s + r.count, 0)} pts
              </span>
            </button>

            {expanded[date] && byDate[date].sort((a, b) => b.hour.localeCompare(a.hour)).map(run => {
              const key = `${run.date}/${run.hour}/${run.mode}`
              const isSelected = selectedRunId === key
              const pts = patients[key] || []

              return (
                <div key={key}>
                  {/* Hour bucket */}
                  <button
                    onClick={() => selectRun(run)}
                    className={`w-full text-left px-6 py-2 text-xs transition-colors border-l-2 ${
                      isSelected
                        ? 'border-l-accent bg-accent/5 text-white'
                        : 'border-l-transparent text-gray-400 hover:text-white hover:bg-gray-800/40'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-mono">{run.hour}:00</span>
                      <div className="flex items-center gap-2">
                        <span className="text-gray-500">{run.count} pts</span>
                        <span className="text-yellow-500 font-mono">{fmtUsd(run.cost_usd)}</span>
                      </div>
                    </div>
                    <div className="text-gray-600 mt-0.5">{run.mode}</div>
                  </button>

                  {/* Patient list within this run */}
                  {isSelected && (
                    <div className="bg-gray-900/60">
                      {pLoading[key] && (
                        <div className="px-8 py-2 text-xs text-gray-500">Loading…</div>
                      )}
                      {pts.map(p => (
                        <button
                          key={p.run_id}
                          onClick={() => onSelectPatient(p.run_id)}
                          className={`w-full text-left px-8 py-2.5 border-b border-gray-800/50 transition-colors ${
                            selectedPatientId === p.run_id
                              ? 'bg-gray-700/50 text-white'
                              : 'text-gray-400 hover:bg-gray-800/50 hover:text-white'
                          }`}
                        >
                          <div className="flex items-center justify-between gap-1">
                            <span className="text-xs font-medium truncate">
                              {p.unit} {p.bed}
                            </span>
                            <span className="text-[10px] text-yellow-500 font-mono flex-shrink-0">
                              {fmtUsd(p.cost_usd)}
                            </span>
                          </div>
                          <div className="text-[10px] text-gray-500 truncate mt-0.5">{p.cpmrn}</div>
                          <div className="text-[10px] text-gray-600 truncate">{p.brief}</div>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Inputs tab ────────────────────────────────────────────────────────────────

function InputsTab({ entry }) {
  const { sections = {}, meta = {} } = entry
  const vitals = parseVitals(sections.vitals)
  const labs   = parseLabs(sections.labs)
  const longit = parseLongitudinal(sections.longitudinal)

  return (
    <div className="flex flex-col gap-4 px-6 py-5 overflow-y-auto h-full">
      {/* Header */}
      <div className="bg-gray-800 rounded-lg px-4 py-3">
        <div className="text-white font-semibold">{meta.brief || '—'}</div>
        <div className="flex flex-wrap gap-4 mt-2 text-xs text-gray-400">
          <span>CPMRN: <span className="text-white font-mono">{meta.cpmrn}</span></span>
          <span>Unit: <span className="text-white">{meta.unit}</span></span>
          <span>Bed: <span className="text-white">{meta.bed}</span></span>
          <span>Mode: <span className="text-white">{entry.mode}</span></span>
          <span>Model: <span className="text-white font-mono">{entry.model}</span></span>
          <span>KB: <span className="text-white">{entry.kb}</span></span>
        </div>
      </div>

      {/* Problem list */}
      {longit?.problems?.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Problem List</h3>
          <div className="flex flex-col gap-2">
            {longit.problems.map((p, i) => (
              <div key={i} className={`rounded-lg px-4 py-3 ${STATUS_COLOR[p.status] || 'bg-gray-800/60 text-gray-300'}`}>
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-semibold">{p.name}</span>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium uppercase tracking-wide ${STATUS_COLOR[p.status] || 'bg-gray-700 text-gray-400'}`}>
                    {p.status}
                  </span>
                </div>
                {p.current_state && <p className="text-xs opacity-80">{p.current_state}</p>}
                {p.management && <p className="text-xs opacity-60 mt-0.5">Rx: {p.management}</p>}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Suggested actions from context */}
      {longit?.suggested_actions?.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Suggested Actions (prior run)</h3>
          <ul className="text-xs text-gray-300 space-y-1">
            {longit.suggested_actions.map((a, i) => (
              <li key={i} className="flex gap-2"><span className="text-gray-600">•</span>{a}</li>
            ))}
          </ul>
        </section>
      )}

      {/* Vitals */}
      {vitals.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Recent Vitals</h3>
          <div className="flex flex-col gap-1">
            {vitals.map((v, i) => (
              <div key={i} className="flex gap-3 text-xs bg-gray-800/50 rounded px-3 py-1.5">
                <span className="text-gray-500 font-mono flex-shrink-0 w-36">
                  {new Date(v.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </span>
                <span className="text-gray-300 font-mono">{v.vals}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Labs */}
      {labs.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Recent Labs</h3>
          <div className="flex flex-col gap-1">
            {labs.map((l, i) => (
              <div key={i} className="flex gap-3 text-xs bg-gray-800/50 rounded px-3 py-1.5">
                <span className="text-gray-500 font-mono flex-shrink-0 w-36">
                  {new Date(l.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', month: 'short', day: 'numeric' })}
                </span>
                <span className="text-gray-300 font-mono">{l.vals}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Orders */}
      {sections.orders && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Active Orders</h3>
          <pre className="text-xs text-gray-400 bg-gray-800/50 rounded px-3 py-2 whitespace-pre-wrap leading-relaxed">
            {sections.orders}
          </pre>
        </section>
      )}

      {/* Fluid balance */}
      {sections.fluid && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Fluid Balance</h3>
          <pre className="text-xs text-gray-400 bg-gray-800/50 rounded px-3 py-2 whitespace-pre-wrap">
            {sections.fluid}
          </pre>
        </section>
      )}
    </div>
  )
}

// ── Thought stream tab ────────────────────────────────────────────────────────

function CostChip({ cost, tokens }) {
  if (!cost && !tokens) return null
  return (
    <div className="flex items-center gap-2 text-[10px] font-mono">
      {cost != null && (
        <span className="text-yellow-500 bg-yellow-900/30 px-1.5 py-0.5 rounded">{fmtUsd(cost)}</span>
      )}
      {tokens && (
        <span className="text-gray-500">
          {fmtTokens(tokens.input_tokens)}↑ {fmtTokens(tokens.output_tokens)}↓
        </span>
      )}
    </div>
  )
}

function StepCard({ number, title, color = 'border-blue-600', children, cost, tokens, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className={`border-l-2 ${color} pl-4`}>
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-2 w-full text-left mb-1"
      >
        <span className="text-[10px] font-mono bg-gray-700 text-gray-400 rounded px-1.5 py-0.5 flex-shrink-0">
          {number}
        </span>
        <span className="text-sm font-medium text-white">{title}</span>
        <div className="ml-auto flex items-center gap-2">
          <CostChip cost={cost} tokens={tokens} />
          {open
            ? <ChevronDownIcon className="w-3.5 h-3.5 text-gray-500" />
            : <ChevronRightIcon className="w-3.5 h-3.5 text-gray-500" />
          }
        </div>
      </button>
      {open && <div className="mt-2">{children}</div>}
    </div>
  )
}

function ThoughtStreamTab({ entry }) {
  const { step1 = {}, step2 = {}, step3 = {}, costs = {} } = entry

  return (
    <div className="flex flex-col gap-5 px-6 py-5 overflow-y-auto h-full">

      {/* Step 1: Clinical analysis */}
      <StepCard
        number="Step 1"
        title="Clinical Analysis"
        color="border-blue-500"
        cost={costs.step1}
        tokens={{ input_tokens: step1.input_tokens, output_tokens: step1.output_tokens }}
        defaultOpen
      >
        {step1.clinical_reasoning?.length > 0 && (
          <div className="mb-3">
            <h4 className="text-[10px] uppercase tracking-widest text-gray-500 mb-1.5">Reasoning</h4>
            <div className="space-y-1.5">
              {step1.clinical_reasoning.map((r, i) => (
                <p key={i} className="text-xs text-gray-300 bg-gray-800/60 rounded px-3 py-2 leading-relaxed">{r}</p>
              ))}
            </div>
          </div>
        )}

        {step1.clinical_direction?.length > 0 && (
          <div className="mb-3">
            <h4 className="text-[10px] uppercase tracking-widest text-gray-500 mb-1.5">Clinical Direction</h4>
            <ul className="space-y-1">
              {step1.clinical_direction.map((d, i) => (
                <li key={i} className="flex gap-2 text-xs text-gray-300">
                  <span className="text-blue-500 flex-shrink-0">→</span>{d}
                </li>
              ))}
            </ul>
          </div>
        )}

        {step1.specific_queries?.length > 0 && (
          <div>
            <h4 className="text-[10px] uppercase tracking-widest text-gray-500 mb-1.5">Wiki Queries Planned</h4>
            <div className="space-y-1">
              {step1.specific_queries.map((q, i) => (
                <div key={i} className="text-xs bg-indigo-900/30 border border-indigo-800/40 rounded px-3 py-1.5">
                  <span className="text-indigo-300 font-medium">{q.parameter}</span>
                  <span className="text-gray-500 ml-2">→ "{q.search_query}"</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {step1.conditional_orders?.length > 0 && (
          <div className="mt-3">
            <h4 className="text-[10px] uppercase tracking-widest text-gray-500 mb-1.5">Conditional Orders</h4>
            {step1.conditional_orders.map((o, i) => (
              <div key={i} className="text-xs bg-gray-800/50 rounded px-3 py-2 mb-1.5">
                <div className="text-yellow-300 font-medium">{o.summary}</div>
                <div className="text-gray-400 mt-0.5">If: {o.condition}</div>
                <div className="text-gray-300 mt-0.5">Then: {o.action}</div>
              </div>
            ))}
          </div>
        )}
      </StepCard>

      {/* Step 2: Wiki retrieval */}
      {step2.retrievals?.length > 0 && (
        <StepCard
          number="Step 2"
          title="Wiki Retrieval"
          color="border-purple-500"
          cost={costs.step2}
          tokens={{ input_tokens: step2.input_tokens, output_tokens: step2.output_tokens }}
          defaultOpen
        >
          <div className="space-y-2">
            {step2.retrievals.map((r, i) => (
              <div key={i} className="bg-gray-800/50 rounded-lg px-3 py-2">
                <div className="flex items-start justify-between gap-2 mb-1.5">
                  <span className="text-xs font-medium text-purple-300">{r.parameter}</span>
                  <span className="text-[10px] font-mono text-gray-500 flex-shrink-0">
                    best: {r.top_score?.toFixed(3)}
                  </span>
                </div>
                <div className="text-[10px] text-gray-500 mb-1.5 font-mono">"{r.search_query}"</div>
                {r.sections_searched?.slice(0, 4).map((s, j) => (
                  <div key={j} className="flex items-center gap-2 text-[10px] py-0.5">
                    <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                      s.score > 0.7 ? 'bg-green-400' : s.score > 0.5 ? 'bg-yellow-400' : 'bg-gray-600'
                    }`} />
                    <span className="text-gray-400 truncate font-mono">{s.path}</span>
                    <span className="text-gray-600 ml-auto flex-shrink-0">§ {s.section}</span>
                    <span className={`font-mono flex-shrink-0 ${
                      s.score > 0.7 ? 'text-green-400' : s.score > 0.5 ? 'text-yellow-400' : 'text-gray-600'
                    }`}>{s.score?.toFixed(3)}</span>
                  </div>
                ))}
                {r.sections_searched?.length > 4 && (
                  <div className="text-[10px] text-gray-600 mt-1">
                    +{r.sections_searched.length - 4} more sections
                  </div>
                )}
              </div>
            ))}
          </div>
        </StepCard>
      )}

      {/* Step 3: Synthesis */}
      {step3.raw_steps?.length > 0 && (
        <StepCard
          number="Step 3"
          title="Synthesis (Reasoning Steps)"
          color="border-green-500"
          cost={costs.step3}
          tokens={{ input_tokens: step3.input_tokens, output_tokens: step3.output_tokens }}
          defaultOpen
        >
          <ol className="space-y-2">
            {step3.raw_steps.map((step, i) => (
              <li key={i} className="flex gap-3 text-xs">
                <span className="text-green-600 font-mono flex-shrink-0 mt-0.5">{i + 1}.</span>
                <span className="text-gray-300 leading-relaxed">{step}</span>
              </li>
            ))}
          </ol>
        </StepCard>
      )}
    </div>
  )
}

// ── Output tab ────────────────────────────────────────────────────────────────

function OutputTab({ entry }) {
  const { final = {}, costs = {}, tokens = {} } = entry
  const steps = entry.step1 || {}

  return (
    <div className="flex flex-col gap-4 px-6 py-5 overflow-y-auto h-full">

      {/* Final recommendations */}
      {final.immediate_next_steps?.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Immediate Next Steps</h3>
          <ol className="space-y-2">
            {final.immediate_next_steps.map((s, i) => (
              <li key={i} className="flex gap-3 text-sm">
                <span className="text-accent font-mono font-bold flex-shrink-0 mt-0.5">{i + 1}.</span>
                <span className="text-gray-200 leading-relaxed">{s}</span>
              </li>
            ))}
          </ol>
        </section>
      )}

      {/* Monitoring */}
      {steps.monitoring_followup?.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Monitoring & Follow-up</h3>
          <ul className="space-y-1">
            {steps.monitoring_followup.map((m, i) => (
              <li key={i} className="flex gap-2 text-xs text-gray-300">
                <span className="text-gray-600">•</span>{m}
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Pages consulted */}
      {final.pages_consulted?.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Wiki Pages Consulted</h3>
          <div className="flex flex-wrap gap-1.5">
            {final.pages_consulted.map((p, i) => (
              <span key={i} className="text-[10px] bg-gray-800 text-gray-400 rounded px-2 py-1 font-mono">
                {p}
              </span>
            ))}
          </div>
        </section>
      )}

      {/* Knowledge gaps */}
      {final.ungrounded_params?.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Knowledge Gaps</h3>
          <div className="space-y-1">
            {final.ungrounded_params.map((g, i) => (
              <div key={i} className="text-xs text-orange-300 bg-orange-900/20 rounded px-3 py-1.5">
                ⚠ {g}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Gaps registered */}
      {final.gaps_registered?.length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Gaps Filed</h3>
          <div className="space-y-1">
            {final.gaps_registered.map((g, i) => (
              <div key={i} className="text-xs text-gray-400 bg-gray-800/50 rounded px-3 py-1.5 font-mono">{g}</div>
            ))}
          </div>
        </section>
      )}

      {/* Cost breakdown */}
      <section className="mt-2">
        <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">Cost Breakdown</h3>
        <div className="bg-gray-800 rounded-lg overflow-hidden">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-gray-700">
                <th className="text-left px-4 py-2 text-gray-500 font-medium">Step</th>
                <th className="text-right px-4 py-2 text-gray-500 font-medium">In</th>
                <th className="text-right px-4 py-2 text-gray-500 font-medium">Out</th>
                <th className="text-right px-4 py-2 text-gray-500 font-medium">Cost (est.)</th>
              </tr>
            </thead>
            <tbody>
              {[
                { label: 'Step 1 · Clinical Analysis', key: 'step1', color: 'text-blue-400' },
                { label: 'Step 2 · Wiki Retrieval',   key: 'step2', color: 'text-purple-400' },
                { label: 'Step 3 · Synthesis',        key: 'step3', color: 'text-green-400' },
              ].map(({ label, key, color }) => {
                const s = entry[key] || {}
                return (
                  <tr key={key} className="border-b border-gray-700/50">
                    <td className={`px-4 py-2 ${color}`}>{label}</td>
                    <td className="px-4 py-2 text-right text-gray-400 font-mono">
                      {fmtTokens(s.input_tokens)}
                    </td>
                    <td className="px-4 py-2 text-right text-gray-400 font-mono">
                      {fmtTokens(s.output_tokens)}
                    </td>
                    <td className="px-4 py-2 text-right text-yellow-400 font-mono">
                      {fmtUsd(costs[key])}
                    </td>
                  </tr>
                )
              })}
              <tr className="bg-gray-700/30">
                <td className="px-4 py-2 text-white font-semibold">Total</td>
                <td className="px-4 py-2 text-right text-white font-mono">
                  {fmtTokens(tokens.input)}
                </td>
                <td className="px-4 py-2 text-right text-white font-mono">
                  {fmtTokens(tokens.output)}
                </td>
                <td className="px-4 py-2 text-right text-yellow-300 font-mono font-bold">
                  {fmtUsd(costs.total)}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="text-[10px] text-gray-600 mt-1 px-1">
          Step costs are estimated proportionally from total; totals are exact.
        </p>
      </section>
    </div>
  )
}

// ── Patient detail panel ──────────────────────────────────────────────────────

function PatientDetail({ runId }) {
  const [entry, setEntry]   = useState(null)
  const [loading, setLoading] = useState(false)
  const [tab, setTab]       = useState('stream')

  useEffect(() => {
    if (!runId) return
    setEntry(null)
    setLoading(true)
    getTraceEntry(runId)
      .then(setEntry)
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [runId])

  if (!runId) return (
    <div className="flex items-center justify-center h-full text-gray-600 text-sm">
      Select a patient from the run
    </div>
  )

  if (loading) return (
    <div className="flex items-center justify-center h-full text-gray-500 text-sm">Loading…</div>
  )

  if (!entry) return (
    <div className="flex items-center justify-center h-full text-gray-600 text-sm">
      Entry not found
    </div>
  )

  const TABS = [
    { id: 'inputs', label: 'Inputs' },
    { id: 'stream', label: 'Thought Stream' },
    { id: 'output', label: 'Output' },
  ]

  return (
    <div className="flex flex-col h-full">
      {/* Tab bar */}
      <div className="flex-shrink-0 border-b border-gray-700 flex items-center gap-1 px-4 pt-3">
        {TABS.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 text-xs font-medium rounded-t transition-colors border-b-2 ${
              tab === t.id
                ? 'text-white border-accent'
                : 'text-gray-500 border-transparent hover:text-gray-300'
            }`}
          >
            {t.label}
          </button>
        ))}
        <div className="ml-auto flex items-center gap-3 pb-2 text-xs text-gray-500">
          <span className="font-mono">{entry.meta?.cpmrn}</span>
          <span>{relTime(entry.timestamp)}</span>
          <span className="text-yellow-500 font-mono font-bold">
            {fmtUsd(entry.costs?.total)}
          </span>
        </div>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-hidden">
        {tab === 'inputs' && <InputsTab entry={entry} />}
        {tab === 'stream' && <ThoughtStreamTab entry={entry} />}
        {tab === 'output' && <OutputTab entry={entry} />}
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Traces() {
  const [selectedRunId, setSelectedRunId]           = useState(null)
  const [selectedPatientId, setSelectedPatientId]   = useState(null)

  return (
    <div className="flex h-full overflow-hidden bg-gray-900">
      {/* Left — run + patient list */}
      <div className="w-72 flex-shrink-0 border-r border-gray-700 flex flex-col overflow-hidden">
        <RunList
          selectedRunId={selectedRunId}
          onSelectRun={setSelectedRunId}
          selectedPatientId={selectedPatientId}
          onSelectPatient={setSelectedPatientId}
        />
      </div>

      {/* Right — patient detail */}
      <div className="flex-1 overflow-hidden">
        <PatientDetail runId={selectedPatientId} />
      </div>
    </div>
  )
}
