import { useState, useEffect } from 'react'
import { PencilIcon, TrashIcon, CheckIcon, XMarkIcon, PlusIcon } from '@heroicons/react/24/outline'
import { listClinicalRules, createClinicalRule, updateClinicalRule, deleteClinicalRule } from '../api'
import { useAppState } from '../AppStateContext'

const EMPTY_FORM = { enabled: true, triggers: '', rule: '' }

function RuleRow({ rule, onRefresh }) {
  const { activeKB } = useAppState()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({
    enabled: rule.enabled,
    triggers_text: (rule.triggers || []).join(', '),
    rule: rule.rule,
  })
  const [saving, setSaving] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState(null)

  function startEdit() {
    setDraft({ enabled: rule.enabled, triggers_text: (rule.triggers || []).join(', '), rule: rule.rule })
    setEditing(true)
    setError(null)
  }

  async function handleSave() {
    setSaving(true)
    setError(null)
    try {
      const triggers = draft.triggers_text
        ? draft.triggers_text.split(',').map(s => s.trim()).filter(Boolean)
        : []
      await updateClinicalRule(rule.id, { enabled: draft.enabled, triggers, rule: draft.rule.trim() }, activeKB)
      setEditing(false)
      onRefresh()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  async function handleDelete() {
    setDeleting(true)
    setError(null)
    try {
      await deleteClinicalRule(rule.id, activeKB)
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
            value={draft.triggers_text}
            onChange={e => setDraft(d => ({ ...d, triggers_text: e.target.value }))}
            placeholder="Trigger keywords (comma-separated) — leave empty for always-on"
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent"
          />
          <textarea
            rows={3}
            value={draft.rule}
            onChange={e => setDraft(d => ({ ...d, rule: e.target.value }))}
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-accent resize-none"
          />
          <div className="flex items-center gap-2">
            <button
              onClick={handleSave}
              disabled={saving || !draft.rule.trim()}
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
            <div className="flex items-center flex-wrap gap-1.5 mb-2">
              <span className="font-mono text-[10px] text-muted/60">{rule.id}</span>
              {rule.enabled
                ? <span className="px-1.5 py-0.5 rounded text-[9px] border bg-green-500/10 border-green-500/30 text-green-400 uppercase tracking-wide">on</span>
                : <span className="px-1.5 py-0.5 rounded text-[9px] border bg-zinc-700/40 border-zinc-600 text-muted uppercase tracking-wide">off</span>
              }
              {(!rule.triggers || rule.triggers.length === 0) && (
                <span className="px-1.5 py-0.5 rounded text-[9px] border bg-accent/10 border-accent/30 text-accent uppercase tracking-wide">always-on</span>
              )}
            </div>
            {rule.triggers && rule.triggers.length > 0 && (
              <div className="flex flex-wrap gap-1 mb-2">
                {rule.triggers.map(t => (
                  <span key={t} className="px-1.5 py-0.5 rounded text-[10px] bg-ink-800 border border-border text-muted">
                    {t}
                  </span>
                ))}
              </div>
            )}
            <p className="text-sm text-white leading-relaxed">{rule.rule}</p>
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

export default function ClinicalRules() {
  const { activeKB } = useAppState()
  const [rules, setRules] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [showAdd, setShowAdd] = useState(false)
  const [addForm, setAddForm] = useState(EMPTY_FORM)
  const [saving, setSaving] = useState(false)

  function fetchRules() {
    setLoading(true)
    setError(null)
    listClinicalRules(activeKB)
      .then(d => { setRules(d.rules || []); setLoading(false) })
      .catch(e => { setError(e.message); setLoading(false) })
  }

  useEffect(() => { fetchRules() }, [activeKB])

  async function handleAdd() {
    if (!addForm.rule.trim()) return
    setSaving(true)
    setError(null)
    try {
      const triggers = addForm.triggers
        ? addForm.triggers.split(',').map(s => s.trim()).filter(Boolean)
        : []
      await createClinicalRule({ enabled: addForm.enabled, triggers, rule: addForm.rule.trim() }, activeKB)
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
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-white mb-1">
            Clinical Rules
            <span className="ml-2 text-accent font-mono text-sm font-normal">{activeKB}</span>
          </h1>
          <p className="text-sm text-muted max-w-xl">
            Guardrails injected into Step 1 CDS reasoning. Trigger keywords are matched
            case-insensitively against the clinical question.
            Rules with no triggers are always active.
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
            placeholder="Trigger keywords (comma-separated) — leave empty for always-on"
            value={addForm.triggers}
            onChange={e => setAddForm(f => ({ ...f, triggers: e.target.value }))}
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent"
          />
          <textarea
            rows={3}
            placeholder="Rule text injected verbatim into the Step 1 prompt…"
            value={addForm.rule}
            onChange={e => setAddForm(f => ({ ...f, rule: e.target.value }))}
            className="w-full bg-ink-800 border border-border rounded px-3 py-2 text-sm text-white placeholder:text-muted/50 focus:outline-none focus:border-accent resize-none"
          />
          <div className="flex items-center gap-2">
            <button
              onClick={handleAdd}
              disabled={saving || !addForm.rule.trim()}
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
