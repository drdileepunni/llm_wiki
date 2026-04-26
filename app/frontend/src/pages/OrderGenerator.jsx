import { useState } from 'react'
import { generateOrders } from '../api'
import { useAppState } from '../AppStateContext'

const TYPE_COLORS = {
  med:        'bg-blue-500/15 text-blue-300 border-blue-500/30',
  lab:        'bg-purple-500/15 text-purple-300 border-purple-500/30',
  procedure:  'bg-amber-500/15 text-amber-300 border-amber-500/30',
  monitoring: 'bg-teal-500/15 text-teal-300 border-teal-500/30',
}
const CONFIDENCE_COLORS = {
  high:   'text-green-400',
  medium: 'text-amber-400',
  low:    'text-red-400',
}
const CONFIDENCE_DOT = {
  high:   'bg-green-400',
  medium: 'bg-amber-400',
  low:    'bg-red-400',
}

function OrderCard({ order }) {
  const typeCls = TYPE_COLORS[order.order_type] || TYPE_COLORS.monitoring
  const confCls = CONFIDENCE_COLORS[order.confidence] || CONFIDENCE_COLORS.low
  const dotCls  = CONFIDENCE_DOT[order.confidence]  || CONFIDENCE_DOT.low

  const details = order.order_details || {}
  const detailRows = [
    details.name      && ['Drug / Item', details.name],
    details.quantity  && details.unit && ['Dose', `${details.quantity} ${details.unit}`],
    details.route     && ['Route', details.route],
    details.form      && ['Form', details.form],
    details.frequency && ['Frequency', details.frequency],
    details.instructions && ['Instructions', details.instructions],
  ].filter(Boolean)

  return (
    <div className="border border-border rounded-lg p-4 space-y-3 bg-ink-900">
      {/* Header row */}
      <div className="flex items-start justify-between gap-3">
        <p className="text-sm text-white/80 leading-snug flex-1">{order.recommendation}</p>
        <span className={`text-[10px] font-mono uppercase px-2 py-0.5 rounded border flex-shrink-0 ${typeCls}`}>
          {order.order_type}
        </span>
      </div>

      {/* Orderable name */}
      {order.orderable_name && (
        <p className="text-sm font-medium text-white">{order.orderable_name}</p>
      )}

      {/* Detail table */}
      {detailRows.length > 0 && (
        <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          {detailRows.map(([label, value]) => (
            <>
              <span key={`l-${label}`} className="text-muted">{label}</span>
              <span key={`v-${label}`} className="text-white font-mono">{value}</span>
            </>
          ))}
        </div>
      )}

      {/* Dose calculation */}
      {order.dose_calculation && (
        <p className="text-xs font-mono text-accent/80 bg-accent/5 px-2 py-1 rounded">
          {order.dose_calculation}
        </p>
      )}

      {/* Footer: confidence + notes */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className={`flex items-center gap-1.5 text-xs ${confCls}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${dotCls}`} />
          {order.confidence} confidence
        </span>
        {order.notes && (
          <span className="text-xs text-muted">· {order.notes}</span>
        )}
      </div>
    </div>
  )
}

const PLACEHOLDER = `Vancomycin 10mg/kg twice daily IV
Continuously monitor SpO2, targeting a goal of > 92%
Ringer's lactate 100 mL/hr IV
CBC with differential
Monitor urine output > 0.5 mL/kg/hr`

export default function OrderGenerator() {
  const { activeKB } = useAppState()
  const [text, setText] = useState('')
  const [cpmrn, setCpmrn] = useState('')
  const [patientType, setPatientType] = useState('adult')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  async function handleGenerate() {
    const recs = text
      .split('\n')
      .map(l => l.trim())
      .filter(Boolean)

    if (!recs.length) return

    setLoading(true)
    setError(null)
    setResult(null)

    try {
      const data = await generateOrders({
        recommendations: recs,
        cpmrn: cpmrn.trim() || null,
        patientType,
      }, activeKB)
      setResult(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const orders = result?.orders || []

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-8 py-5 border-b border-border flex items-center justify-between">
        <div>
          <h1 className="font-display text-2xl font-semibold text-white">Order Generator</h1>
          <p className="text-sm text-muted mt-0.5">
            Convert clinical recommendations to structured EMR orders
          </p>
        </div>
        {result && (
          <p className="text-xs text-muted font-mono">
            ${result.cost_usd?.toFixed(4)} · {result.input_tokens + result.output_tokens} tok
          </p>
        )}
      </div>

      {/* Body: two columns */}
      <div className="flex-1 overflow-hidden flex gap-0">

        {/* Left: Input */}
        <div className="w-80 flex-shrink-0 border-r border-border flex flex-col p-5 gap-4">
          <div className="space-y-1.5">
            <label className="text-xs text-muted uppercase tracking-widest">
              Recommendations
            </label>
            <textarea
              value={text}
              onChange={e => setText(e.target.value)}
              placeholder={PLACEHOLDER}
              rows={12}
              className="w-full bg-ink-800 border border-border rounded px-3 py-2.5 text-sm text-white placeholder:text-muted/50 resize-none focus:outline-none focus:border-accent font-mono leading-relaxed"
            />
            <p className="text-[10px] text-muted">One recommendation per line</p>
          </div>

          <div className="space-y-1.5">
            <label className="text-xs text-muted uppercase tracking-widest">
              CPMRN <span className="normal-case">(optional — for weight-based dosing)</span>
            </label>
            <input
              value={cpmrn}
              onChange={e => setCpmrn(e.target.value)}
              placeholder="e.g. INTSNLG2851387"
              className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent font-mono"
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-xs text-muted uppercase tracking-widest">Patient Type</label>
            <select
              value={patientType}
              onChange={e => setPatientType(e.target.value)}
              className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-accent"
            >
              <option value="adult">Adult</option>
              <option value="pediatric">Pediatric</option>
              <option value="neonatal">Neonatal</option>
            </select>
          </div>

          <button
            onClick={handleGenerate}
            disabled={loading || !text.trim()}
            className="w-full py-2.5 rounded-md bg-accent text-white text-sm font-medium hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            {loading ? 'Generating…' : 'Generate Orders'}
          </button>
        </div>

        {/* Right: Output */}
        <div className="flex-1 overflow-y-auto p-5">
          {loading && (
            <div className="flex items-center gap-3 text-muted text-sm">
              <span className="animate-pulse">Searching orderables catalog…</span>
            </div>
          )}

          {error && (
            <div className="border border-red-500/30 bg-red-500/10 rounded-lg px-4 py-3 text-sm text-red-300">
              {error}
            </div>
          )}

          {!loading && !error && orders.length === 0 && (
            <div className="text-muted text-sm">
              Paste recommendations on the left and click Generate Orders.
            </div>
          )}

          {orders.length > 0 && (
            <div className="space-y-3">
              <p className="text-xs text-muted uppercase tracking-widest mb-4">
                {orders.length} order{orders.length !== 1 ? 's' : ''} generated
              </p>

              {result?.weight_gap_registered && (
                <div className="border border-amber-500/30 bg-amber-500/8 rounded-lg px-4 py-3 text-xs text-amber-300 mb-4">
                  <span className="font-medium">Knowledge gap registered: </span>
                  <span className="font-mono">{result.weight_gap_registered}</span>
                  <span className="text-amber-300/70 ml-2">— standard weight data is missing from the wiki. Resolve this gap to enable accurate weight-based dosing.</span>
                </div>
              )}

              {orders.map((order, i) => (
                <OrderCard key={i} order={order} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
