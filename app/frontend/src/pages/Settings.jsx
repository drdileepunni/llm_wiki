import { useState, useEffect } from 'react'
import { getGchatWebhook, saveGchatWebhook, testGchatWebhook } from '../api'
import {
  BellAlertIcon,
  CheckCircleIcon,
  ExclamationCircleIcon,
} from '@heroicons/react/24/outline'

export default function Settings() {
  const [url, setUrl] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [toast, setToast] = useState(null) // { type: 'ok'|'err', msg }

  useEffect(() => {
    getGchatWebhook()
      .then(d => { setUrl(d.url || ''); setEnabled(d.enabled || false) })
      .catch(() => flash('err', 'Failed to load settings'))
      .finally(() => setLoading(false))
  }, [])

  function flash(type, msg) {
    setToast({ type, msg })
    setTimeout(() => setToast(null), 4000)
  }

  async function handleSave() {
    setSaving(true)
    try {
      await saveGchatWebhook(url.trim(), enabled)
      flash('ok', 'Settings saved')
    } catch (e) {
      flash('err', e.message)
    } finally {
      setSaving(false)
    }
  }

  async function handleTest() {
    if (!url.trim()) { flash('err', 'Enter a webhook URL first'); return }
    setTesting(true)
    try {
      await saveGchatWebhook(url.trim(), enabled)   // save first so backend uses latest URL
      await testGchatWebhook()
      flash('ok', 'Test message sent — check your Google Chat space')
    } catch (e) {
      flash('err', e.message || 'Webhook delivery failed')
    } finally {
      setTesting(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64 text-gray-400 text-sm">
        Loading settings…
      </div>
    )
  }

  return (
    <div className="max-w-2xl mx-auto p-6 space-y-8">
      <h1 className="text-lg font-semibold text-gray-100">Settings</h1>

      {/* Google Chat Alerts */}
      <section className="bg-gray-800 rounded-xl p-6 space-y-5 border border-gray-700">
        <div className="flex items-center gap-2">
          <BellAlertIcon className="w-5 h-5 text-orange-400" />
          <h2 className="font-medium text-gray-100">Google Chat Alerts</h2>
        </div>
        <p className="text-sm text-gray-400">
          When a patient's condition is <span className="text-orange-400 font-medium">worsening</span> or{' '}
          <span className="text-red-400 font-medium">critical</span>, the CDS pipeline fires and posts
          a structured alert card to this Google Chat webhook.
        </p>

        {/* URL input */}
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-gray-400 uppercase tracking-wide">
            Webhook URL
          </label>
          <input
            type="url"
            value={url}
            onChange={e => setUrl(e.target.value)}
            placeholder="https://chat.googleapis.com/v1/spaces/…"
            className="w-full bg-gray-900 border border-gray-600 rounded-lg px-3 py-2 text-sm
                       text-gray-100 placeholder-gray-600 focus:outline-none focus:border-orange-500
                       focus:ring-1 focus:ring-orange-500 font-mono"
          />
        </div>

        {/* Enable toggle */}
        <div className="flex items-center justify-between">
          <span className="text-sm text-gray-300">Enable alerts</span>
          <button
            onClick={() => setEnabled(v => !v)}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors
              ${enabled ? 'bg-orange-500' : 'bg-gray-600'}`}
          >
            <span
              className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform
                ${enabled ? 'translate-x-6' : 'translate-x-1'}`}
            />
          </button>
        </div>

        {/* Trigger note */}
        <p className="text-xs text-gray-500">
          Alerts fire automatically during hourly pipeline runs when any problem is worsening or critical.
          CDS suggestions are skipped and no alert is sent when all problems are stable or improving.
        </p>

        {/* Actions */}
        <div className="flex items-center gap-3 pt-1">
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-2 bg-orange-600 hover:bg-orange-500 disabled:opacity-50
                       text-white text-sm rounded-lg transition-colors"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
          <button
            onClick={handleTest}
            disabled={testing || !url.trim()}
            className="px-4 py-2 bg-gray-700 hover:bg-gray-600 disabled:opacity-40
                       text-gray-200 text-sm rounded-lg transition-colors"
          >
            {testing ? 'Sending…' : 'Send test message'}
          </button>
        </div>
      </section>

      {/* Toast */}
      {toast && (
        <div className={`fixed bottom-6 right-6 flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm
          ${toast.type === 'ok' ? 'bg-green-800 text-green-100' : 'bg-red-800 text-red-100'}`}>
          {toast.type === 'ok'
            ? <CheckCircleIcon className="w-4 h-4 shrink-0" />
            : <ExclamationCircleIcon className="w-4 h-4 shrink-0" />}
          {toast.msg}
        </div>
      )}
    </div>
  )
}
