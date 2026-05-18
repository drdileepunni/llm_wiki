import { useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'

// ═══════════════════════════════════════════════════════════════════════════════
// Shared layout + render helpers
// ═══════════════════════════════════════════════════════════════════════════════

const NODE_W       = 162
const NODE_H       = 58
const COL_W        = 196
const LANE_H       = 108
const LANE_GAP     = 40
const LANE_LABEL_H = 20
const MARGIN_X     = 28
const MARGIN_Y     = 28

function computeLayout(rowDefs) {
  const maxCols = Math.max(...rowDefs.map(r => r.nodeIds.length))
  const totalW  = MARGIN_X * 2 + maxCols * COL_W
  const totalH  = MARGIN_Y * 2 + rowDefs.length * LANE_H + (rowDefs.length - 1) * LANE_GAP
  const pos = {}
  rowDefs.forEach((row, rowIdx) => {
    const laneY = MARGIN_Y + rowIdx * (LANE_H + LANE_GAP)
    const nodeY = laneY + LANE_LABEL_H + (LANE_H - LANE_LABEL_H - NODE_H) / 2
    row.nodeIds.forEach((id, colIdx) => {
      const nodeX = MARGIN_X + colIdx * COL_W + (COL_W - NODE_W) / 2
      pos[id] = {
        x: nodeX, y: nodeY,
        cx: nodeX + NODE_W / 2,
        cy: nodeY + NODE_H / 2,
        right:  nodeX + NODE_W,
        bottom: nodeY + NODE_H,
        rowIdx, colIdx, laneY,
      }
    })
  })
  return { pos, totalW, totalH }
}

function edgePath(e, pos) {
  const f = pos[e.from], t = pos[e.to]
  if (!f || !t) return ''

  if (e.arcAbove) {
    const arcY = Math.min(f.laneY, t.laneY) - 22
    const sx = f.cx, sy = f.y
    const tx = t.cx, ty = t.y
    return `M ${sx} ${sy} C ${sx + 40} ${arcY}, ${tx - 40} ${arcY}, ${tx} ${ty}`
  }

  if (e.cross) {
    const sx = f.cx, sy = f.bottom
    const tx = t.x,  ty = t.cy
    const midY = (sy + ty) / 2
    return `M ${sx} ${sy} C ${sx} ${midY}, ${tx} ${midY}, ${tx} ${ty}`
  }

  const sx = f.right, sy = f.cy
  const tx = t.x,    ty = t.cy
  const cx = (sx + tx) / 2
  return `M ${sx} ${sy} C ${cx} ${sy}, ${cx} ${ty}, ${tx} ${ty}`
}

function edgeLabelPos(e, pos) {
  const f = pos[e.from], t = pos[e.to]
  if (!f || !t) return null
  if (e.arcAbove) return { x: (f.cx + t.cx) / 2, y: Math.min(f.laneY, t.laneY) - 28 }
  if (e.cross)    return { x: f.cx, y: (f.bottom + t.cy) / 2 - 6 }
  return { x: (f.right + t.x) / 2, y: f.cy - 8 }
}

function Tooltip({ node, pos: mousePos, svgRect }) {
  if (!node || !mousePos) return null
  const TIP_W = 320
  const rawLeft = mousePos.x + 16
  const left = rawLeft + TIP_W > (svgRect?.width ?? 9999) ? mousePos.x - TIP_W - 8 : rawLeft
  const top  = Math.min(mousePos.y, window.innerHeight - 400)
  return (
    <div
      style={{ position: 'fixed', left, top, width: TIP_W, zIndex: 100, pointerEvents: 'none' }}
      className="bg-ink-800 border border-border rounded-xl shadow-2xl p-4 text-xs font-body"
    >
      <p className="font-semibold text-white text-sm mb-1">{node.tooltip.title}</p>
      <p className="text-muted leading-relaxed mb-3">{node.tooltip.body}</p>
      {node.tooltip.fields?.length > 0 && (
        <div className="space-y-1 mb-3">
          {node.tooltip.fields.map(f => (
            <div key={f.k} className="flex gap-2">
              <span className="font-mono text-accent shrink-0">{f.k}</span>
              <span className="text-muted">{f.v}</span>
            </div>
          ))}
        </div>
      )}
      {node.tooltip.tools?.length > 0 && (
        <div className="mb-2">
          <p className="text-[10px] uppercase tracking-widest text-muted mb-1.5">Tools available</p>
          <div className="flex flex-wrap gap-1">
            {node.tooltip.tools.map(t => (
              <span key={t} className="px-1.5 py-0.5 bg-ink-700 rounded font-mono text-[10px] text-accent">{t}</span>
            ))}
          </div>
        </div>
      )}
      {node.tooltip.outputs?.length > 0 && (
        <div>
          <p className="text-[10px] uppercase tracking-widest text-muted mb-1.5">Outputs</p>
          <div className="flex flex-wrap gap-1">
            {node.tooltip.outputs.map(o => (
              <span key={o} className="px-1.5 py-0.5 bg-ink-700 rounded font-mono text-[10px] text-green-400">{o}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function renderDiagram(svgRef, nodes, edges, rowDefs, lanes, setHovered, setSvgRect) {
  const laneColor = Object.fromEntries(lanes.map(l => [l.id, l.color]))
  const { pos, totalW, totalH } = computeLayout(rowDefs)

  const svg = d3.select(svgRef.current)
  svg.selectAll('*').remove()
  svg.attr('width', '100%').attr('height', '100%')

  const zoom = d3.zoom().scaleExtent([0.15, 3]).on('zoom', e => root.attr('transform', e.transform))
  svg.call(zoom)
  const root = svg.append('g')

  const defs = svg.append('defs')
  ;['solid', 'dashed'].forEach(style => {
    defs.append('marker')
      .attr('id', `arr-${style}`)
      .attr('viewBox', '0 0 10 10').attr('refX', 9).attr('refY', 5)
      .attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto')
      .append('path').attr('d', 'M 0 0 L 10 5 L 0 10 z')
      .attr('fill', style === 'dashed' ? '#fbbf24' : '#4b4b6a')
  })

  rowDefs.forEach((row, rowIdx) => {
    const lane = lanes.find(l => l.id === row.laneId)
    if (!lane) return
    const laneY = MARGIN_Y + rowIdx * (LANE_H + LANE_GAP)
    const laneW = totalW - MARGIN_X

    root.append('rect')
      .attr('x', MARGIN_X / 2).attr('y', laneY)
      .attr('width', laneW).attr('height', LANE_H)
      .attr('rx', 10)
      .attr('fill', lane.color).attr('fill-opacity', 0.06)
      .attr('stroke', lane.color).attr('stroke-opacity', 0.25).attr('stroke-width', 1.5)

    root.append('text')
      .attr('x', MARGIN_X / 2 + 10).attr('y', laneY + 15)
      .attr('fill', lane.color).attr('fill-opacity', 0.75)
      .attr('font-size', 9.5).attr('font-family', 'JetBrains Mono, monospace')
      .attr('letter-spacing', 1.2)
      .text(lane.label.toUpperCase())
  })

  edges.forEach(e => {
    const isDashed = !!e.dashed
    const col = isDashed ? '#fbbf24' : '#4b4b6a'
    const d = edgePath(e, pos)
    if (!d) return
    root.append('path')
      .attr('d', d).attr('fill', 'none')
      .attr('stroke', col).attr('stroke-width', 1.5)
      .attr('stroke-dasharray', isDashed ? '5,4' : null)
      .attr('marker-end', `url(#arr-${isDashed ? 'dashed' : 'solid'})`)
    if (e.label) {
      const lp = edgeLabelPos(e, pos)
      if (lp) {
        root.append('text')
          .attr('x', lp.x).attr('y', lp.y)
          .attr('fill', isDashed ? '#fbbf24' : '#6b6b8a')
          .attr('font-size', 9).attr('text-anchor', 'middle')
          .attr('font-family', 'JetBrains Mono, monospace')
          .text(e.label)
      }
    }
  })

  const nodeData = nodes.map(n => ({ ...n, ...pos[n.id] })).filter(n => n.x != null)
  const ng = root.selectAll('g.node').data(nodeData).join('g')
    .attr('class', 'node')
    .attr('transform', d => `translate(${d.x}, ${d.y})`)
    .style('cursor', 'pointer')

  ng.append('rect')
    .attr('width', NODE_W).attr('height', NODE_H).attr('rx', 8)
    .attr('fill', '#1a1a24')
    .attr('stroke', d => {
      if (d.type === 'decision') return '#fbbf24'
      if (d.type === 'feedback') return '#f87171'
      if (d.type === 'io')       return '#4b4b6a'
      return laneColor[d.lane] || '#7c6af7'
    })
    .attr('stroke-width', d => d.type === 'artifact' ? 2 : 1.5)
    .attr('stroke-dasharray', d => d.type === 'io' ? '4,3' : null)

  ng.filter(d => d.type === 'process' || d.type === 'feedback')
    .append('rect')
    .attr('width', 3).attr('height', NODE_H - 16).attr('x', 0).attr('y', 8).attr('rx', 2)
    .attr('fill', d => d.type === 'feedback' ? '#f87171' : (laneColor[d.lane] || '#7c6af7'))

  ng.append('text')
    .attr('x', d => (d.type === 'process' || d.type === 'feedback') ? 11 : 8)
    .attr('y', 22)
    .attr('fill', '#e8e8f0').attr('font-size', 12).attr('font-weight', 600)
    .attr('font-family', 'DM Sans, sans-serif')
    .text(d => d.label)

  ng.append('text')
    .attr('x', d => (d.type === 'process' || d.type === 'feedback') ? 11 : 8)
    .attr('y', 38)
    .attr('fill', '#6b6b8a').attr('font-size', 10)
    .attr('font-family', 'JetBrains Mono, monospace')
    .text(d => d.sublabel)

  ng.filter(d => d.type === 'decision')
    .append('text')
    .attr('x', NODE_W - 7).attr('y', 14)
    .attr('fill', '#fbbf24').attr('font-size', 9).attr('text-anchor', 'end')
    .attr('font-family', 'JetBrains Mono, monospace')
    .text('branch')

  ng.on('mouseenter', function(event, d) {
      d3.select(this).select('rect').transition().duration(100).attr('fill', '#252533')
      setSvgRect(svgRef.current?.getBoundingClientRect())
      setHovered({ node: d, pos: { x: event.clientX, y: event.clientY } })
    })
    .on('mousemove', function(event) {
      setHovered(h => h ? { ...h, pos: { x: event.clientX, y: event.clientY } } : h)
    })
    .on('mouseleave', function() {
      d3.select(this).select('rect').transition().duration(100).attr('fill', '#1a1a24')
      setHovered(null)
    })

  requestAnimationFrame(() => {
    const el = svgRef.current
    if (!el) return
    const cw = el.clientWidth  || 900
    const ch = el.clientHeight || 500
    const pad = 24
    const scale = Math.min((cw - pad * 2) / totalW, (ch - pad * 2) / totalH)
    if (!isFinite(scale) || scale <= 0) return
    const tx = (cw - totalW * scale) / 2
    const ty = (ch - totalH * scale) / 2
    svg.call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(scale))
  })
}

// ═══════════════════════════════════════════════════════════════════════════════
// CDS Pipeline — node / edge / lane definitions
// ═══════════════════════════════════════════════════════════════════════════════

const CDS_NODES = [
  {
    id: 'input', label: 'Clinical Question', sublabel: 'context + question',
    lane: 'phase1', type: 'io',
    tooltip: {
      title: 'Input',
      body: 'The clinical context and question passed in per turn. In the AB pipeline this is constructed from the scenario + simulation state. In a live Viva session it comes from the teacher.',
      fields: [
        { k: 'clinical_context', v: 'What is happening clinically' },
        { k: 'question',         v: 'What the CDS must answer' },
        { k: 'cpmrn',            v: 'Patient identifier for chart lookups' },
      ],
    },
  },
  {
    id: 'tool_loop', label: 'ReAct Tool Loop', sublabel: 'Phase 1 · ≤6 rounds',
    lane: 'phase1', type: 'process',
    tooltip: {
      title: 'Phase 1 — Patient Data Collection',
      body: 'LLM acts as a senior ICU clinician querying the live chart. Decides which tools to call and in what order. Runs up to 6 rounds then forced to write the data brief.',
      fields: [
        { k: 'Model',       v: 'Claude Sonnet (default)' },
        { k: 'Max rounds',  v: '6' },
        { k: 'System prompt', v: 'Senior ICU clinician — retrieve only what you need' },
      ],
      tools: ['get_patient_history', 'get_vitals', 'get_lab_result', 'get_io_summary', 'get_vital_trend', 'get_recent_notes', 'get_active_orders'],
    },
  },
  {
    id: 'data_brief', label: 'Patient Data Brief', sublabel: '200–300 word handover',
    lane: 'phase1', type: 'artifact',
    tooltip: {
      title: 'Patient Data Brief',
      body: 'Structured clinical handover written by the LLM from tool results. Shared by both arms in AB testing — produced once, consumed twice.',
      fields: [
        { k: 'Primary Syndrome',   v: 'Dominant problem + severity' },
        { k: 'Key Abnormalities',  v: 'Bulleted with actual values' },
        { k: 'Renal Function',     v: 'Creatinine, eGFR — mandatory for dosing' },
        { k: 'Diagnoses / PMH',    v: 'Chronic conditions — affects contraindications' },
        { k: 'Home Medications',   v: 'Pre-admission meds — tolerance and interactions' },
        { k: 'Active Medications', v: 'Currently running' },
        { k: 'Current Status',     v: 'Trajectory and most urgent concern' },
      ],
    },
  },
  {
    id: 'step1a', label: 'Step 1A · Reasoning', sublabel: 'MedGemma · clinical judgment',
    lane: 'cds', type: 'process',
    tooltip: {
      title: 'Step 1A — Clinical Reasoning (no wiki)',
      body: 'Reasoning LLM (MedGemma or capable model) reads the assembled question and reasons from medical training alone. No wiki access. Outputs only simple string arrays so MedGemma/Ollama models work reliably without native tool use.',
      fields: [
        { k: 'Model',          v: 'REASONING_MODEL env var (default: main model)' },
        { k: 'Thinking budget', v: '4 000 tokens (Gemini/Claude); 0 (MedGemma — ignored)' },
        { k: 'Guardrails',     v: 'clinical_rules.yaml injected' },
        { k: 'Conditionals',   v: 'Pending if-then orders from prior turns' },
        { k: 'Split trigger',  v: 'Automatic when model is MedGemma/Ollama' },
      ],
      outputs: ['clinical_direction', 'clinical_reasoning', 'monitoring_followup', 'alternative_considerations'],
    },
  },
  {
    id: 'step1b', label: 'Step 1B · Query Extract', sublabel: 'Main model · structured output',
    lane: 'cds', type: 'process',
    tooltip: {
      title: 'Step 1B — Query & Conditional Extraction',
      body: 'Runs only when Step 1A used MedGemma/Ollama. The main (capable) model reads the clinical direction prose from 1A and extracts the structured wiki search queries and conditional orders. Mechanical extraction — no clinical knowledge required, so MedGemma is not needed here.',
      fields: [
        { k: 'Model',        v: 'Main model (MODEL env var) — always supports tool use' },
        { k: 'Input',        v: 'clinical_direction strings from Step 1A + original question' },
        { k: 'Single-model path', v: 'Skipped — Gemini/Claude Step 1 produces all 6 fields at once' },
      ],
      outputs: ['specific_queries', 'conditional_orders'],
    },
  },
  {
    id: 'step2_tier1', label: 'Tier 1 · Gap Index', sublabel: 'Resolved shortcut lookup',
    lane: 'cds', type: 'decision',
    tooltip: {
      title: 'Step 2 · Tier 1 — Resolved Gap Index',
      body: 'Before vector search, checks if this parameter was a previously known gap that has been filled. If a shortcut hit exists, goes straight to that page + section.',
      fields: [
        { k: 'Hit',       v: 'Skip vector search — use cached page + section directly' },
        { k: 'Miss',      v: 'Fall through to Tier 2 vector search' },
        { k: 'Side effect', v: 'Increments shortcut_hits counter for observability' },
      ],
    },
  },
  {
    id: 'step2_vector', label: 'Tier 2 · Vector Search', sublabel: 'Per specific_query',
    lane: 'cds', type: 'process',
    tooltip: {
      title: 'Step 2 · Tier 2 — Wiki Vector Search',
      body: 'Runs search_query + parameter name as two queries. Merges hits by best score. If top score < 0.55, retries with a 3-word shortened query. Follows one hop of wiki links.',
      fields: [
        { k: 'Top-k',          v: '10 per query, merged → top 6' },
        { k: 'Min score',      v: '0.55 (below → fallback retry)' },
        { k: 'Link traversal', v: '1 hop, best matching section per linked page' },
      ],
    },
  },
  {
    id: 'step2_ground', label: 'Grounding · LLM verdict', sublabel: 'grounded / not grounded',
    lane: 'cds', type: 'process',
    tooltip: {
      title: 'Step 2 — Parameter Grounding',
      body: 'LLM reads each retrieved wiki section and decides if the value is present and applicable. Can call get_latest_lab for severity classification tables (e.g. hyperkalemia grades).',
      fields: [
        { k: 'grounded=true',    v: 'Wiki has a directly applicable value' },
        { k: 'grounded=false',   v: 'Value is genuinely absent from wiki' },
        { k: 'Score skepticism', v: 'Extra skeptical when retrieval score < 0.65' },
        { k: 'Agentic lab lookup', v: 'Up to 3 get_latest_lab calls for severity tiers' },
      ],
      tools: ['get_latest_lab', 'get_patient_demographics'],
    },
  },
  {
    id: 'step3', label: 'Step 3 · Synthesise', sublabel: '5–8 executable steps',
    lane: 'cds', type: 'process',
    tooltip: {
      title: 'Step 3 — Immediate Next Steps',
      body: 'LLM takes clinical_direction + grounded parameter values → 5–8 fully executable, prioritised bedside actions. Only wiki-grounded values used. Ungrounded → marker.',
      fields: [
        { k: 'Output',    v: 'immediate_next_steps[]' },
        { k: 'Ordering',  v: 'Clinical urgency — life threats first' },
        { k: 'Marker',    v: '"(value not in wiki)" for every ungrounded param' },
        { k: 'Exclusion', v: 'conditional_orders excluded (cached for future turns)' },
      ],
    },
  },
  {
    id: 'gap_reg', label: 'Gap Registration', sublabel: 'async · self-improvement',
    lane: 'cds', type: 'feedback',
    tooltip: {
      title: 'Gap Registration — Async Self-Improvement Loop',
      body: "Fires as a background thread after Step 3 — does not block the clinical response. For every ungrounded parameter and remaining marker, files a structured knowledge gap. The mop-up pipeline fills them — the wiki grows from every question it can't answer.",
      fields: [
        { k: 'Trigger',             v: 'grounded=false params + "(value not in wiki)" markers after Step 3' },
        { k: 'Execution',           v: 'Background thread (daemon=True) — non-blocking' },
        { k: 'entity',              v: 'Drug / device / condition' },
        { k: 'section_heading',     v: 'Wiki section that should contain the value' },
        { k: 'resolution_question', v: 'Precise clinical question whose answer fills the gap' },
        { k: 'placement',           v: 'confirmed (score ≥ 0.70) or approximate' },
      ],
    },
  },
  {
    id: 'og_phase0', label: 'Order Gen · Phase 0', sublabel: 'Intent extraction',
    lane: 'orders', type: 'process',
    tooltip: {
      title: 'Order Gen Phase 0 — Intent Extraction',
      body: 'Single forced LLM call. Reads all recommendation strings and maps each orderable entity to a clean catalog_query + order_type. One recommendation can produce multiple intents.',
      fields: [
        { k: 'Rules',   v: 'order_rules.json — keyword → catalog_query mapping' },
        { k: 'Output',  v: 'intents[]: {index, catalog_query, order_type, parameters}' },
        { k: 'Lab split', v: '"check creatinine, K+, BNP" → 3 separate intents' },
      ],
    },
  },
  {
    id: 'og_phase1', label: 'Order Gen · Phase 1', sublabel: 'Data gathering · ≤8 iters',
    lane: 'orders', type: 'process',
    tooltip: {
      title: 'Order Gen Phase 1 — Data Gathering Loop',
      body: 'LLM searches EMR catalog and fetches patient data. CKD renal dosing rule: if PMH has CKD and no creatinine, impute conservative CrCl from stage. Weight: actual EMR → IBW from height → wiki default → gap.',
      fields: [
        { k: 'Max iterations', v: '8' },
        { k: 'CKD imputed CrCl', v: 'Stage 1→90, 2→60, 3→30, 3a→45, 4→15, 5→10 mL/min' },
        { k: 'Weight chain',   v: 'Actual EMR → IBW from height → wiki default → gap' },
        { k: 'Active orders',  v: 'Compared → action = edit / stop / new' },
      ],
      tools: ['search_orderables', 'create_orderable', 'get_patient_demographics', 'get_latest_lab'],
    },
  },
  {
    id: 'og_phase2', label: 'Order Gen · Phase 2', sublabel: 'Force submit_orders',
    lane: 'orders', type: 'process',
    tooltip: {
      title: 'Order Gen Phase 2 — Submit Orders',
      body: 'LLM forced (no other tools) to call submit_orders once. Reminder injected with exact orderable names from Phase 1 and Phase 0 parameter extractions so vent settings and drug doses are correctly populated.',
      fields: [
        { k: 'order_details',    v: '{quantity, unit, route, frequency, instructions}' },
        { k: 'dose_calculation', v: 'e.g. "1 mg/kg × 70 kg = 70 mg"' },
        { k: 'confidence',       v: 'high / medium / low' },
        { k: 'action',           v: 'new / edit / stop' },
      ],
    },
  },
  {
    id: 'orders_out', label: 'Structured Orders', sublabel: 'Written to MongoDB',
    lane: 'orders', type: 'io',
    tooltip: {
      title: 'Output — Structured Orders',
      body: "Final orders placed into the patient's MongoDB chart via place_viva_order(). In the AB pipeline, Arm A's orders exclusively drive the simulator into the next turn.",
      fields: [
        { k: 'Storage',  v: 'MongoDB (AB_DUMMY_CPMRN)' },
        { k: 'AB role',  v: "Arm A orders drive simulator; Arm B's recorded only" },
        { k: 'Next turn', v: 'simulate_and_write() reads these to evolve patient state' },
      ],
    },
  },
]

const CDS_EDGES = [
  { from: 'input',        to: 'tool_loop' },
  { from: 'tool_loop',    to: 'data_brief' },
  { from: 'data_brief',   to: 'step1a',       cross: true },
  { from: 'step1a',       to: 'step1b' },
  { from: 'step1b',       to: 'step2_tier1' },
  { from: 'step2_tier1',  to: 'step2_vector', label: 'miss' },
  { from: 'step2_tier1',  to: 'step2_ground', label: 'hit', arcAbove: true },
  { from: 'step2_vector', to: 'step2_ground' },
  { from: 'step2_ground', to: 'step3' },
  { from: 'step3',        to: 'gap_reg',    label: 'async' },
  { from: 'step3',        to: 'og_phase0',  cross: true },
  { from: 'gap_reg',      to: 'step2_tier1', label: 'next session', dashed: true, arcAbove: true },
  { from: 'og_phase0',    to: 'og_phase1' },
  { from: 'og_phase1',    to: 'og_phase2' },
  { from: 'og_phase2',    to: 'orders_out' },
]

const CDS_LANES = [
  { id: 'phase1', label: 'Phase 1 · Data Collection',       color: '#0ea5e9' },
  { id: 'cds',    label: 'Phase 2 · CDS Synthesis (Arm A)', color: '#7c6af7' },
  { id: 'orders', label: 'Order Generation',                color: '#4ade80' },
]

const CDS_ROW_DEFS = [
  { laneId: 'phase1', nodeIds: ['input', 'tool_loop', 'data_brief'] },
  { laneId: 'cds',    nodeIds: ['step1a', 'step1b', 'step2_tier1', 'step2_vector', 'step2_ground', 'step3', 'gap_reg'] },
  { laneId: 'orders', nodeIds: ['og_phase0', 'og_phase1', 'og_phase2', 'orders_out'] },
]

// ═══════════════════════════════════════════════════════════════════════════════
// Mop-up Pipeline — node / edge / lane definitions
// ═══════════════════════════════════════════════════════════════════════════════

const MOPUP_LANES = [
  { id: 'scoring',   label: 'Scoring · Importance Ranking',            color: '#0ea5e9' },
  { id: 'expansion', label: 'Stub Expansion · Per-Page Loop',           color: '#7c6af7' },
  { id: 'defrag',    label: 'Defrag · Scope Contamination (optional)',  color: '#f97316' },
  { id: 'finalize',  label: 'Finalise',                                 color: '#6b7280' },
]

const MOPUP_NODES = [
  // ── Scoring lane ─────────────────────────────────────────────────────────
  {
    id: 'wiki_pages', label: 'Wiki Pages', sublabel: 'entities/ + concepts/',
    lane: 'scoring', type: 'io',
    tooltip: {
      title: 'Input — Wiki Pages',
      body: 'All markdown pages in wiki/entities/ and wiki/concepts/. For the agent_school KB this is ~1 300 pages. Sources, patients, and queries are excluded from scoring.',
      fields: [
        { k: 'Scanned fields', v: 'title, subtype, scope, section headings, body word count' },
        { k: 'Excluded',       v: 'sources/, patients/, queries/' },
      ],
    },
  },
  {
    id: 'score_compute', label: 'Score Computation', sublabel: 'CDS queries + inbound × 2',
    lane: 'scoring', type: 'process',
    tooltip: {
      title: 'Composite Importance Score',
      body: 'Each page is scored as: cds_query_count + inbound_link_count × 2. Pages heavily queried by the CDS engine or linked by many other pages are prioritised first. No LLM calls — pure file scan.',
      fields: [
        { k: 'cds_query_count', v: 'from page_metrics.json — incremented each time CDS retrieves this page' },
        { k: 'inbound_link_count', v: 'scanned from all wiki markdown — [[Page Title]] references' },
        { k: 'inbound weight × 2', v: 'links are a proxy for clinical centrality, weighted higher' },
      ],
    },
  },
  {
    id: 'stub_filter', label: 'Stub Filter', sublabel: 'words < 300  ·  score ≥ 20',
    lane: 'scoring', type: 'decision',
    tooltip: {
      title: 'Two-Condition Stub Filter',
      body: 'A page must be both thin (stub) AND important (high score) to enter the expansion queue. Low-score stubs are ignored — not worth the LLM cost. Both thresholds are configurable in the Mop-up UI.',
      fields: [
        { k: 'word_threshold',  v: 'default 300 — pages below this count as stubs' },
        { k: 'score_threshold', v: 'default 20 — pages below this are low-priority, skipped' },
        { k: 'max_stubs',       v: 'default 50 per run — safety cap on LLM calls' },
      ],
    },
  },
  {
    id: 'stub_queue', label: 'Stub Queue', sublabel: 'ranked by importance',
    lane: 'scoring', type: 'artifact',
    tooltip: {
      title: 'Stub Queue',
      body: 'Ordered list of pages to expand in this run — most important first. Visible in the Stub Queue tab before running. The subtype shown is heuristic (fast); the LLM ✦ AI button can refine it per row.',
      fields: [
        { k: 'Displayed', v: 'title, subtype, score, CDS count, inbound links, word count' },
        { k: 'Subtype badge', v: '↑inferred = heuristic guess; manual edit or ✦ AI to correct' },
      ],
    },
  },

  // ── Expansion lane ────────────────────────────────────────────────────────
  {
    id: 'read_page', label: 'Read Page', sublabel: 'frontmatter + body',
    lane: 'expansion', type: 'process',
    tooltip: {
      title: 'Read Page',
      body: 'Reads the page markdown file. Parses YAML frontmatter (title, subtype, scope, section_quality) and scans existing ## headings to determine what sections already exist.',
      fields: [
        { k: 'Reads', v: 'title, subtype, scope, existing section headings, body text' },
        { k: 'Skips', v: 'pages that have been modified in the last 24 h (safety guard)' },
      ],
    },
  },
  {
    id: 'subtype_resolve', label: 'Subtype Resolve', sublabel: 'known / heuristic / ✦ AI',
    lane: 'expansion', type: 'decision',
    tooltip: {
      title: 'Subtype Resolution',
      body: 'Determines the structural type that controls which section template is used. Three strategies in order of cost: use the existing frontmatter value → fast heuristic → on-demand MedGemma call.',
      fields: [
        { k: 'known',     v: 'subtype already set in frontmatter — used as-is' },
        { k: 'heuristic', v: 'scope keyword match (pharmacology, dosing, …) + drug name suffix patterns — instant, no LLM' },
        { k: '✦ AI',      v: 'manual MedGemma call from UI — max 10 tokens, saves result to frontmatter' },
        { k: 'subtypes',  v: 'medication · parameter · investigation · procedure · condition · default' },
      ],
    },
  },
  {
    id: 'template_select', label: 'Template Select', sublabel: 'section list for subtype',
    lane: 'expansion', type: 'process',
    tooltip: {
      title: 'Template Selection',
      body: 'Fetches the canonical ordered section list for the resolved subtype from page_templates.py. Each subtype has a curated set of sections tuned for ICU clinical use.',
      fields: [
        { k: 'medication', v: 'Mechanism of Action, Indications, Dosing, Renal/Hepatic/Pediatric Dose, Drug Interactions, Monitoring, Adverse Effects, Contraindications, ICU Considerations' },
        { k: 'parameter',  v: 'Definition, Normal Ranges, Clinical Significance, Measurement, ICU Considerations' },
        { k: 'condition',  v: 'Pathophysiology, Diagnosis, Management, ICU Considerations' },
      ],
    },
  },
  {
    id: 'missing_secs', label: 'Missing Sections', sublabel: 'diff vs template',
    lane: 'expansion', type: 'process',
    tooltip: {
      title: 'Missing Section Diff',
      body: 'Compares the template section list against the existing ## headings in the page. Only sections that are absent are queued for generation — existing sections are never overwritten.',
      fields: [
        { k: 'existing', v: 'parsed from ## headings in current page' },
        { k: 'missing',  v: 'template sections not present — these go to MedGemma' },
        { k: 'skipped',  v: 'sections already present — left untouched' },
      ],
    },
  },
  {
    id: 'medgemma', label: 'MedGemma Prompt', sublabel: 'local · KG_FALLBACK_MODEL',
    lane: 'expansion', type: 'process',
    tooltip: {
      title: 'MedGemma — Section Generation',
      body: 'One LLM call per page (not per section). System prompt establishes the medical reference author role with scope constraint and wiki-link style rules. User prompt lists all missing sections with per-section instructions.',
      fields: [
        { k: 'Model',          v: 'KG_FALLBACK_MODEL — MedGemma via Ollama' },
        { k: 'Scope constraint', v: 'injected per page — LLM may not write outside scope' },
        { k: 'Table rule',     v: 'dosing, renal adjustments, ranges → Markdown tables' },
        { k: 'Link rule',      v: '[[Page Title]] for cross-references' },
        { k: 'Numeric values', v: 'specific doses, thresholds, ranges required' },
      ],
    },
  },
  {
    id: 'merge_embed', label: 'Merge + Re-embed', sublabel: 'write · close gaps · vector store',
    lane: 'expansion', type: 'process',
    tooltip: {
      title: 'Merge, Gap Close, Re-embed',
      body: 'Parsed sections from the MedGemma response are appended to the page. Gap files pointing to this page are resolved. The page is re-embedded in the vector store so the CDS engine sees the new content immediately.',
      fields: [
        { k: 'Page write',   v: 'new sections appended to page markdown' },
        { k: 'Gap files',    v: 'open gap files for this page closed — prevents repeated re-generation' },
        { k: 'Vector store', v: 'page chunk embeddings updated in-place' },
        { k: 'Next run',     v: 'page now exceeds word threshold — graduates out of queue' },
      ],
    },
  },

  // ── Defrag lane ───────────────────────────────────────────────────────────
  {
    id: 'scope_scan', label: 'Scope Scan', sublabel: 'LLM section check (manual)',
    lane: 'defrag', type: 'process',
    tooltip: {
      title: 'Scope Scan — Manual Trigger',
      body: 'An LLM reads each section of every entity/concept page and checks whether it belongs within that page\'s declared scope. This is intentionally manual — it makes one LLM call per page and is expensive to run on the full wiki.',
      fields: [
        { k: 'Trigger', v: 'manual — Scope Contamination tab → Scan all pages' },
        { k: 'Output',  v: 'scope_contamination: true/false in frontmatter + violation list' },
        { k: 'False positives', v: 'can be whitelisted per section via ✕ button in UI' },
      ],
    },
  },
  {
    id: 'contamination', label: 'Contamination?', sublabel: 'scope_contamination flag',
    lane: 'defrag', type: 'decision',
    tooltip: {
      title: 'Contamination Branch',
      body: 'Pages with scope_contamination: true in frontmatter are queued for defrag. Pages without the flag pass through immediately to finalize.',
      fields: [
        { k: 'true',  v: 'one or more sections belong on a different page → defrag' },
        { k: 'false', v: 'page is clean → skip to finalize' },
      ],
    },
  },
  {
    id: 'move_content', label: 'Move Content', sublabel: 'misplaced → correct page',
    lane: 'defrag', type: 'process',
    tooltip: {
      title: 'Content Migration',
      body: 'For each contaminated section, the LLM identifies the correct target page and moves the content there. The source section is replaced with a one-line cross-reference. Content is never deleted.',
      fields: [
        { k: 'Method',   v: 'LLM identifies belongs_on target from violation record' },
        { k: 'Source',   v: 'section replaced with "See [[Target Page]] for details."' },
        { k: 'Target',   v: 'content appended to correct page' },
        { k: 'Preserves', v: 'wiki links, original content, no data loss' },
      ],
    },
  },
  {
    id: 'gap_safety', label: 'Gap Safety', sublabel: 'resolve_gap_sections on target',
    lane: 'defrag', type: 'process',
    tooltip: {
      title: 'Gap Index Safety',
      body: 'After content is moved to a target page, resolve_gap_sections() is called on that target. This ensures knowledge gap embeddings pointing into the target page remain valid — Tier 1 gap index lookups will still find the right sections.',
      fields: [
        { k: 'Calls',   v: 'resolve_gap_sections(target_path)' },
        { k: 'Updates', v: 'resolved_gap_index.json — semantic gap cache' },
        { k: 'Why',     v: 'prevents stale embeddings from the Tier 1 shortcut cache' },
      ],
    },
  },

  // ── Finalize lane ─────────────────────────────────────────────────────────
  {
    id: 'sync_index', label: 'Sync Index', sublabel: 'index.md + page_metrics',
    lane: 'finalize', type: 'process',
    tooltip: {
      title: 'Sync Index',
      body: 'Updates index.md with any modified or newly expanded pages. Refreshes page_metrics.json with updated word counts and section counts so the next scoring run reflects the current state.',
      fields: [
        { k: 'index.md',        v: 'page catalog — new entries added, titles updated' },
        { k: 'page_metrics.json', v: 'word count, section count, subtype updated per page' },
      ],
    },
  },
  {
    id: 'activity_log', label: 'Activity Log', sublabel: 'activity.jsonl entry',
    lane: 'finalize', type: 'io',
    tooltip: {
      title: 'Activity Log',
      body: 'Appends a mopup event to activity.jsonl. This event is visible in the Activity feed and the Last Result tab of the Mop-up UI.',
      fields: [
        { k: 'operation',       v: '"mopup"' },
        { k: 'stubs_expanded',  v: 'count of pages that received new sections' },
        { k: 'sections_added',  v: 'total sections written across all pages' },
        { k: 'stubs_skipped',   v: 'pages already clean or unable to expand' },
        { k: 'defrag_ran',      v: 'true / false' },
      ],
    },
  },
]

const MOPUP_EDGES = [
  { from: 'wiki_pages',      to: 'score_compute'   },
  { from: 'score_compute',   to: 'stub_filter'      },
  { from: 'stub_filter',     to: 'stub_queue'       },
  { from: 'stub_queue',      to: 'read_page',        cross: true },
  { from: 'read_page',       to: 'subtype_resolve'  },
  { from: 'subtype_resolve', to: 'template_select'  },
  { from: 'template_select', to: 'missing_secs'     },
  { from: 'missing_secs',    to: 'medgemma'         },
  { from: 'medgemma',        to: 'merge_embed'      },
  { from: 'merge_embed',     to: 'scope_scan',       cross: true },
  { from: 'scope_scan',      to: 'contamination'    },
  { from: 'contamination',   to: 'move_content',    label: 'yes' },
  { from: 'move_content',    to: 'gap_safety'       },
  { from: 'gap_safety',      to: 'sync_index',       cross: true },
  { from: 'sync_index',      to: 'activity_log'     },
  { from: 'merge_embed',     to: 'wiki_pages',       dashed: true, arcAbove: true },
]

const MOPUP_ROW_DEFS = [
  { laneId: 'scoring',   nodeIds: ['wiki_pages', 'score_compute', 'stub_filter', 'stub_queue'] },
  { laneId: 'expansion', nodeIds: ['read_page', 'subtype_resolve', 'template_select', 'missing_secs', 'medgemma', 'merge_embed'] },
  { laneId: 'defrag',    nodeIds: ['scope_scan', 'contamination', 'move_content', 'gap_safety'] },
  { laneId: 'finalize',  nodeIds: ['sync_index', 'activity_log'] },
]

// ═══════════════════════════════════════════════════════════════════════════════
// Mop-up Glossary
// ═══════════════════════════════════════════════════════════════════════════════

const GLOSSARY = [
  {
    term: 'Wiki Mop-up',
    color: '#7c6af7',
    body: 'A periodic maintenance pipeline that identifies under-developed wiki pages and expands them with structured clinical content. Run on demand from the Mop-up UI. Two phases: stub expansion (add missing sections via MedGemma) and defrag (fix scope contamination).',
  },
  {
    term: 'Stub Page',
    color: '#0ea5e9',
    body: 'A wiki page with fewer than 300 words of body content. Stubs are created automatically when the CDS engine registers a knowledge gap — the page is scaffolded immediately so links don\'t break, but content is left for later. Stubs degrade CDS quality because vector search returns thin pages with no actionable values.',
  },
  {
    term: 'Composite Score',
    color: '#0ea5e9',
    body: 'Importance measure: CDS query count + inbound link count × 2. A page scoring above 20 is actively used in live clinical decisions or referenced by many other wiki pages. Stubs with high scores are the highest-priority expansion targets — they cause the most harm when empty.',
  },
  {
    term: 'Stub Expansion',
    color: '#7c6af7',
    body: 'The process of filling a stub with structured, evidence-based content. MedGemma is prompted with the page\'s subtype and scope to generate each missing section in one call. Existing content is never overwritten — only absent sections are added.',
  },
  {
    term: 'Subtype',
    color: '#7c6af7',
    body: 'Structural classification of a page\'s subject. Controls which section template is used. A medication page gets Dosing, Renal Dose Adjustment, Drug Interactions etc. A condition page gets Pathophysiology, Diagnosis, Management. Types: medication · parameter · investigation · procedure · condition · default.',
  },
  {
    term: 'Scope',
    color: '#7c6af7',
    body: 'A one-sentence constraint in the page\'s frontmatter that tells MedGemma exactly what this page should cover — and no more. Example: "Furosemide: loop diuretic pharmacology, dosing, and monitoring in ICU patients." Without a scope, the LLM tends to write tangential context that belongs on other pages.',
  },
  {
    term: 'Scope Contamination',
    color: '#f97316',
    body: 'When a page accumulates content that belongs on a different page. Example: a Furosemide page that contains a full section on Heart Failure pathophysiology. This happens when MedGemma writes explanatory context instead of staying within scope. Contamination dilutes both pages and adds retrieval noise.',
  },
  {
    term: 'Defrag',
    color: '#f97316',
    body: 'The remediation step for scope contamination. An LLM reads each flagged section, identifies the correct target page, and moves the content there — replacing the source section with a one-line cross-reference. Gap index safety is preserved: resolve_gap_sections() is called on the target so existing gap embeddings remain valid.',
  },
  {
    term: 'Gap Registration',
    color: '#6b7280',
    body: 'When the CDS engine cannot find a wiki value it needs, it registers a knowledge gap. A structured gap file is created recording which page, which section, and what specific clinical question needs answering. Mop-up expansion closes these gap files as part of the merge step — preventing repeated re-generation of content that has already been written.',
  },
]

// ═══════════════════════════════════════════════════════════════════════════════
// Diagram canvas component (reusable)
// ═══════════════════════════════════════════════════════════════════════════════

function DiagramCanvas({ nodes, edges, rowDefs, lanes }) {
  const svgRef  = useRef(null)
  const [hovered,  setHovered]  = useState(null)
  const [svgRect,  setSvgRect]  = useState(null)

  useEffect(() => {
    renderDiagram(svgRef, nodes, edges, rowDefs, lanes, setHovered, setSvgRect)
  }, [])

  return (
    <div className="relative w-full h-full">
      <svg ref={svgRef} style={{ display: 'block', width: '100%', height: '100%' }} />
      <Tooltip node={hovered?.node} pos={hovered?.pos} svgRect={svgRect} />
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Glossary sidebar
// ═══════════════════════════════════════════════════════════════════════════════

function GlossaryPanel() {
  const [open, setOpen] = useState(null)

  return (
    <div className="w-72 flex-shrink-0 flex flex-col border-r border-border bg-ink-900 overflow-y-auto">
      <div className="px-4 py-3 border-b border-border">
        <p className="text-[10px] uppercase tracking-widest text-muted font-mono">Concepts & Definitions</p>
      </div>
      <div className="flex-1 px-3 py-3 space-y-1.5">
        {GLOSSARY.map((g, i) => (
          <div
            key={g.term}
            className="rounded-lg border border-border bg-ink-800/60 overflow-hidden"
          >
            <button
              onClick={() => setOpen(open === i ? null : i)}
              className="w-full flex items-center justify-between px-3 py-2.5 text-left"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span
                  className="w-2 h-2 rounded-full flex-shrink-0"
                  style={{ background: g.color, opacity: 0.8 }}
                />
                <span className="text-xs font-semibold text-white truncate">{g.term}</span>
              </div>
              <span className="text-muted text-[10px] ml-2 flex-shrink-0">
                {open === i ? '▲' : '▼'}
              </span>
            </button>
            {open === i && (
              <div className="px-3 pb-3 pt-0">
                <p className="text-[11px] text-muted leading-relaxed">{g.body}</p>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Shared legend
// ═══════════════════════════════════════════════════════════════════════════════

const LEGEND_ITEMS = [
  { stroke: '#7c6af7', dash: null,  label: 'Process node' },
  { stroke: '#fbbf24', dash: null,  label: 'Decision / branch' },
  { stroke: '#f87171', dash: null,  label: 'Feedback / gap loop' },
  { stroke: '#4b4b6a', dash: '4,3', label: 'I/O boundary' },
  { stroke: '#fbbf24', dash: '5,4', label: 'Cross-session edge' },
]

// ═══════════════════════════════════════════════════════════════════════════════
// Main page
// ═══════════════════════════════════════════════════════════════════════════════

export default function PipelineDocs() {
  const [tab, setTab] = useState('cds')

  return (
    <div className="relative h-full w-full flex flex-col bg-ink-950 overflow-hidden">
      {/* Header */}
      <div className="px-8 py-4 border-b border-border flex items-center justify-between flex-shrink-0">
        <div>
          <h1 className="font-display text-xl text-white">
            {tab === 'cds' ? 'CDS Pipeline Architecture' : 'Wiki Mop-up Pipeline'}
          </h1>
          <p className="text-muted text-xs mt-0.5 font-mono">
            {tab === 'cds'
              ? 'Arm A · Wiki-Grounded · Phase 1 → Phase 2 → Order Generation'
              : 'Periodic wiki maintenance · Stub expansion + Scope contamination fix'}
          </p>
        </div>
        <div className="flex items-center gap-4">
          {/* Tab switcher */}
          <div className="flex rounded-lg border border-border overflow-hidden text-xs font-mono">
            {[
              { id: 'cds',   label: 'CDS Pipeline' },
              { id: 'mopup', label: 'Wiki Mop-up'  },
            ].map(t => (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`px-4 py-1.5 transition-colors ${
                  tab === t.id
                    ? 'bg-accent/20 text-accent'
                    : 'text-muted hover:text-white hover:bg-ink-800'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          {tab === 'cds' && (
            <div className="flex items-center gap-5 text-xs font-mono text-muted">
              {CDS_LANES.map(l => (
                <div key={l.id} className="flex items-center gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-sm" style={{ background: l.color, opacity: 0.8 }} />
                  <span>{l.label}</span>
                </div>
              ))}
            </div>
          )}
          {tab === 'mopup' && (
            <div className="flex items-center gap-4 text-xs font-mono text-muted">
              {MOPUP_LANES.map(l => (
                <div key={l.id} className="flex items-center gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-sm" style={{ background: l.color, opacity: 0.8 }} />
                  <span>{l.label}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Legend */}
      <div className="px-8 py-2 border-b border-border flex items-center gap-6 text-[11px] font-mono text-muted flex-shrink-0">
        {LEGEND_ITEMS.map(l => (
          <div key={l.label} className="flex items-center gap-1.5">
            <svg width="24" height="12">
              <line x1="0" y1="6" x2="24" y2="6" stroke={l.stroke} strokeWidth="1.5" strokeDasharray={l.dash || undefined} />
            </svg>
            <span>{l.label}</span>
          </div>
        ))}
        <span className="ml-auto text-[11px]">scroll to zoom · drag to pan · hover nodes</span>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden flex">
        {tab === 'cds' && (
          <DiagramCanvas
            nodes={CDS_NODES}
            edges={CDS_EDGES}
            rowDefs={CDS_ROW_DEFS}
            lanes={CDS_LANES}
          />
        )}
        {tab === 'mopup' && (
          <>
            <GlossaryPanel />
            <div className="flex-1 overflow-hidden">
              <DiagramCanvas
                nodes={MOPUP_NODES}
                edges={MOPUP_EDGES}
                rowDefs={MOPUP_ROW_DEFS}
                lanes={MOPUP_LANES}
              />
            </div>
          </>
        )}
      </div>
    </div>
  )
}
