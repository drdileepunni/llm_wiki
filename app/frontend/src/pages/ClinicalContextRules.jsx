import { useState, useEffect } from 'react'
import { PencilIcon, TrashIcon, CheckIcon, XMarkIcon, PlusIcon } from '@heroicons/react/24/outline'
import { listContextRules, createContextRule, updateContextRule, deleteContextRule } from '../api'

const EMPTY_FORM = { condition_pattern: '', rule_text: '', reference: '', enabled: true }

function RuleRow({ rule, onRefresh }) {
  const [editing, setEditing]           = useState(false)
  const [draft, setDraft]               = useState({
    condition_pattern: rule.condition_pattern,
    rule_text:         rule.rule_text,
    reference:         rule.reference || '',
    enabled:           rule.enabled,
  })
  const [saving, setSaving]             = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting]         = useState(false)
  const [error, setError]               = useState(null)

  function startEdit() {
    setDraft({
      condition_pattern: rule.condition_pattern,
      rule_text:         rule.rule_text,
      reference:         rule.reference || '',
      enabled:           rule.enabled,
    })
    setEditing(true)
    setError(null)
  }

  async function handleSave() {
    if (!draft.condition_pattern.trim() || !draft.rule_text.trim()) return
    setSaving(true); setError(null)
    try {
      await updateContextRule(rule.id, {
        condition_pattern: draft.condition_pattern.trim(),
        rule_text:         draft.rule_text.trim(),
        reference:         draft.reference.trim(),
        enabled:           draft.enabled,
      })
      setEditing(false)
      onRefresh()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    setDeleting(true); setError(null)
    try {
      await deleteContextRule(rule.id)
      onRefresh()
    } catch (e) {
      setError(e.message)
      setDeleting(false)
      setConfirmDelete(false)
    }
  }

  return (
    <div className={`rounded-lg border bg-ink-900 p-4 space-y-2 transition-opacity ${rule.enabled ? 'border-border' : 'border-border/40 opacity-60'}`}>
      {error && <p className="text-xs text-red-400">{error}</p>}

      {editing ? (
        <div className="space-y-3">
          <label className="flex items-center gap-2 text-sm text-muted cursor-pointer select-none">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={e => setDraft(d => ({ ...d, enabled: e.target.checked }))}
              className="accent-accent"
            />
            Enabled
          </label>
          <input
            value={draft.condition_pattern}
            onChange={e => setDraft(d => ({ ...d, condition_pattern: e.target.value }))}
            placeholder="Condition pattern (e.g. ischemic stroke, copd, raised icp)"
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent"
          />
          <textarea
            rows={4}
            value={draft.rule_text}
            onChange={e => setDraft(d => ({ ...d, rule_text: e.target.value }))}
            placeholder="Rule text injected verbatim into the problem tracker prompt…"
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent resize-none"
          />
          <input
            value={draft.reference}
            onChange={e => setDraft(d => ({ ...d, reference: e.target.value }))}
            placeholder="Reference / guideline (optional)"
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent"
          />
          <div className="flex items-center gap-2">
            <button
              onClick={handleSave}
              disabled={saving || !draft.condition_pattern.trim() || !draft.rule_text.trim()}
              className="p-1.5 rounded text-green-400 hover:bg-green-400/10 disabled:opacity-40 transition-colors"
            >
              <CheckIcon className="w-4 h-4" />
            </button>
            <button
              onClick={() => { setEditing(false); setError(null) }}
              className="p-1.5 rounded text-muted hover:text-white hover:bg-ink-700 transition-colors"
            >
              <XMarkIcon className="w-4 h-4" />
            </button>
          </div>
        </div>
      ) : (
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            {/* ID + badges */}
            <div className="flex items-center flex-wrap gap-1.5 mb-2">
              <span className="font-mono text-[10px] text-muted/60">{rule.id}</span>
              {rule.enabled
                ? <span className="px-1.5 py-0.5 rounded text-[9px] border bg-green-500/10 border-green-500/30 text-green-400 uppercase tracking-wide">on</span>
                : <span className="px-1.5 py-0.5 rounded text-[9px] border bg-zinc-700/40 border-zinc-600 text-muted uppercase tracking-wide">off</span>
              }
            </div>

            {/* Condition pattern chip */}
            <div className="mb-2">
              <span className="px-2 py-0.5 rounded text-[11px] bg-accent/10 border border-accent/30 text-accent font-mono">
                matches: &quot;{rule.condition_pattern}&quot;
              </span>
            </div>

            {/* Rule text */}
            <p className="text-sm text-white leading-relaxed whitespace-pre-wrap">{rule.rule_text}</p>

            {/* Reference */}
            {rule.reference && (
              <p className="mt-2 text-xs text-muted">📖 {rule.reference}</p>
            )}
          </div>

          <div className="flex items-center gap-1 flex-shrink-0 pt-0.5">
            <button onClick={startEdit} className="p-1.5 rounded text-muted/50 hover:text-white hover:bg-ink-700 transition-colors">
              <PencilIcon className="w-4 h-4" />
            </button>
            {confirmDelete ? (
              <>
                <button
                  onClick={handleDelete}
                  disabled={deleting}
                  className="px-2 py-1 rounded text-[11px] text-red-400 bg-red-400/10 hover:bg-red-400/20 transition-colors disabled:opacity-40"
                >
                  {deleting ? '…' : 'Delete'}
                </button>
                <button onClick={() => setConfirmDelete(false)} className="px-2 py-1 rounded text-[11px] text-muted hover:text-white transition-colors">
                  Cancel
                </button>
              </>
            ) : (
              <button onClick={() => setConfirmDelete(true)} className="p-1.5 rounded text-muted/50 hover:text-red-400 hover:bg-red-400/10 transition-colors">
                <TrashIcon className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default function ClinicalContextRules() {
  const [rules, setRules]     = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const [addForm, setAddForm] = useState(EMPTY_FORM)
  const [saving, setSaving]   = useState(false)

  function fetchRules() {
    setLoading(true); setError(null)
    listContextRules()
      .then(d => { setRules(d.rules || []); setLoading(false) })
      .catch(e => { setError(e.message); setLoading(false) })
  }

  useEffect(() => { fetchRules() }, [])

  async function handleAdd() {
    if (!addForm.condition_pattern.trim() || !addForm.rule_text.trim()) return
    setSaving(true); setError(null)
    try {
      await createContextRule({
        condition_pattern: addForm.condition_pattern.trim(),
        rule_text:         addForm.rule_text.trim(),
        reference:         addForm.reference.trim(),
        enabled:           addForm.enabled,
      })
      setAddForm(EMPTY_FORM)
      setShowAdd(false)
      fetchRules()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="p-8 max-w-4xl mx-auto">
      {/* Header */}
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-white mb-1">Clinical Context Rules</h1>
          <p className="text-sm text-muted max-w-xl">
            Condition-specific guardrails injected into the problem tracker when a patient&apos;s
            active problem list matches the condition pattern (case-insensitive substring).
            Use these for permissive thresholds, disease-specific targets, and clinical nuances
            the general alert logic doesn&apos;t know about.
          </p>
        </div>
        <button
          onClick={() => setShowAdd(v => !v)}
          className="flex-shrink-0 flex items-center gap-1.5 px-3 py-1.5 bg-accent text-ink-950 rounded-md text-sm font-medium hover:bg-accent/90 transition-colors"
        >
          <PlusIcon className="w-4 h-4" />
          Add Rule
        </button>
      </div>

      {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

      {/* Add form */}
      {showAdd && (
        <div className="mb-6 rounded-lg border border-accent/40 bg-ink-900 p-4 space-y-3">
          <p className="text-xs font-semibold text-accent uppercase tracking-wider">New Rule</p>
          <label className="flex items-center gap-2 text-sm text-muted cursor-pointer select-none">
            <input
              type="checkbox"
              checked={addForm.enabled}
              onChange={e => setAddForm(f => ({ ...f, enabled: e.target.checked }))}
              className="accent-accent"
            />
            Enabled
          </label>
          <input
            placeholder='Condition pattern — matched against patient problem names (e.g. "ischemic stroke")'
            value={addForm.condition_pattern}
            onChange={e => setAddForm(f => ({ ...f, condition_pattern: e.target.value }))}
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent"
          />
          <textarea
            rows={4}
            placeholder="Rule text — injected verbatim into the problem tracker prompt when this condition is active…"
            value={addForm.rule_text}
            onChange={e => setAddForm(f => ({ ...f, rule_text: e.target.value }))}
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent resize-none"
          />
          <input
            placeholder="Reference / guideline (optional, e.g. AHA/ASA 2019)"
            value={addForm.reference}
            onChange={e => setAddForm(f => ({ ...f, reference: e.target.value }))}
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent"
          />
          <div className="flex items-center gap-2">
            <button
              onClick={handleAdd}
              disabled={saving || !addForm.condition_pattern.trim() || !addForm.rule_text.trim()}
              className="px-3 py-1.5 bg-accent text-ink-950 rounded text-sm font-medium hover:bg-accent/90 disabled:opacity-50 transition-colors"
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
            <button
              onClick={() => { setShowAdd(false); setAddForm(EMPTY_FORM) }}
              className="px-3 py-1.5 text-muted hover:text-white text-sm transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {loading && <p className="text-sm text-muted">Loading…</p>}

      {!loading && rules.length === 0 && (
        <p className="text-sm text-muted">
          No rules yet. Click <strong className="text-white">Add Rule</strong> to create one.
        </p>
      )}

      <div className="space-y-3">
        {rules.map(rule => (
          <RuleRow key={rule.id} rule={rule} onRefresh={fetchRules} />
        ))}
      </div>
    </div>
  )
}
