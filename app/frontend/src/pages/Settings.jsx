import { useState, useEffect } from 'react'
import { getGchatWebhook, saveGchatWebhook, testGchatWebhook, getGchatScraperStatus, syncGchat, getGchatFeedback } from '../api'
import {
  BellAlertIcon,
  CheckCircleIcon,
  ExclamationCircleIcon,
  ArrowPathIcon,
  SignalIcon,
  ChevronDownIcon,
  ChevronRightIcon,
} from '@heroicons/react/24/outline'

export default function Settings() {
  const [url, setUrl] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [toast, setToast] = useState(null) // { type: 'ok'|'err', msg }

  // GChat scraper state
  const [scraperStatus, setScraperStatus] = useState(null)
  const [statusLoading, setStatusLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [feedback, setFeedback] = useState(null) // { total, appropriate, inappropriate, accuracy, rows, new_rows_added? }
  const [expanded, setExpanded] = useState({ appropriate: false, inappropriate: false })

  // Compute daily breakdown from feedback rows
  function getDailyStats(feedback) {
    if (!feedback?.rows) return []
    const byDate = {}
    const allRows = [
      ...feedback.rows.appropriate.map(r => ({ ...r, label: 'appropriate' })),
      ...feedback.rows.inappropriate.map(r => ({ ...r, label: 'inappropriate' })),
    ]
    for (const r of allRows) {
      const date = r.time?.slice(0, 10) ?? 'unknown'
      if (!byDate[date]) byDate[date] = { date, appropriate: 0, inappropriate: 0, total: 0 }
      byDate[date][r.label]++
      byDate[date].total++
    }
    return Object.values(byDate)
      .sort((a, b) => b.date.localeCompare(a.date)) // most recent first
      .map(d => ({ ...d, accuracy: d.total ? Math.round(d.appropriate / d.total * 100) : 0 }))
  }

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

  // Load stored feedback on mount
  useEffect(() => {
    getGchatFeedback().then(setFeedback).catch(() => {})
  }, [])

  async function checkScraperStatus() {
    setStatusLoading(true)
    try {
      const s = await getGchatScraperStatus()
      setScraperStatus(s)
    } catch (e) {
      setScraperStatus({ connected: false, error: e.message })
    } finally {
      setStatusLoading(false)
    }
  }

  async function handleSync() {
    setSyncing(true)
    try {
      const result = await syncGchat()
      setFeedback(result)
      flash('ok', `Synced — ${result.new_rows_added} new row${result.new_rows_added === 1 ? '' : 's'} added`)
    } catch (e) {
      flash('err', e.message || 'Sync failed')
    } finally {
      setSyncing(false)
    }
  }

  function toggleExpanded(key) {
    setExpanded(prev => ({ ...prev, [key]: !prev[key] }))
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

      {/* GChat Feedback Tracker */}
      <section className="bg-gray-800 rounded-xl p-6 space-y-5 border border-gray-700">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <ArrowPathIcon className="w-5 h-5 text-blue-400" />
            <h2 className="font-medium text-gray-100">GChat Alert Feedback</h2>
          </div>
          {feedback && (
            <span className="text-xs text-gray-500">{feedback.total} labelled messages stored</span>
          )}
        </div>

        <p className="text-sm text-gray-400">
          Scrapes the open GChat space and stores messages labelled{' '}
          <span className="text-green-400 font-medium">appropriate</span> /{' '}
          <span className="text-red-400 font-medium">inappropriate</span> by the team.
          New messages are appended; duplicates are skipped.
        </p>

        {/* Connection + Sync row */}
        <div className="flex items-center gap-3 flex-wrap">
          <button
            onClick={checkScraperStatus}
            disabled={statusLoading}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600
                       disabled:opacity-40 text-gray-200 text-sm rounded-lg transition-colors"
          >
            <SignalIcon className="w-4 h-4" />
            {statusLoading ? 'Checking…' : 'Check connection'}
          </button>

          {scraperStatus && (
            <span className={`flex items-center gap-1.5 text-sm ${scraperStatus.connected ? 'text-green-400' : 'text-red-400'}`}>
              {scraperStatus.connected
                ? <><CheckCircleIcon className="w-4 h-4" /> {scraperStatus.browser}</>
                : <><ExclamationCircleIcon className="w-4 h-4" /> Not connected</>}
            </span>
          )}

          <button
            onClick={handleSync}
            disabled={syncing}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500
                       disabled:opacity-50 text-white text-sm rounded-lg transition-colors ml-auto"
          >
            <ArrowPathIcon className={`w-4 h-4 ${syncing ? 'animate-spin' : ''}`} />
            {syncing ? 'Syncing…' : 'Sync latest'}
          </button>
        </div>

        {/* Instructions when not connected */}
        {scraperStatus && !scraperStatus.connected && (
          <div className="bg-gray-900 rounded-lg p-3 text-xs font-mono text-gray-400 space-y-1">
            <p className="text-gray-300 font-sans font-medium text-xs mb-2">Launch Chrome with debugging enabled:</p>
            <p>/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \</p>
            <p className="pl-4">--remote-debugging-port=9222 \</p>
            <p className="pl-4">--user-data-dir="$HOME/chrome-debug-profile"</p>
          </div>
        )}

        {/* Stats */}
        {feedback && feedback.total > 0 && (
          <div className="grid grid-cols-4 gap-3">
            {[
              { label: 'Total', value: feedback.total, color: 'text-gray-100' },
              { label: 'Appropriate', value: feedback.appropriate, color: 'text-green-400' },
              { label: 'Inappropriate', value: feedback.inappropriate, color: 'text-red-400' },
              { label: 'Accuracy', value: `${feedback.accuracy}%`, color: 'text-blue-400' },
            ].map(s => (
              <div key={s.label} className="bg-gray-900 rounded-lg p-3 text-center">
                <div className={`text-2xl font-bold ${s.color}`}>{s.value}</div>
                <div className="text-xs text-gray-500 mt-1">{s.label}</div>
              </div>
            ))}
          </div>
        )}

        {/* Daily breakdown table */}
        {feedback && feedback.total > 0 && (() => {
          const daily = getDailyStats(feedback)
          return (
            <div className="bg-gray-900 rounded-lg overflow-hidden">
              <div className="px-4 py-2.5 border-b border-gray-700">
                <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">Daily Accuracy</span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-gray-500 border-b border-gray-800">
                      <th className="px-4 py-2 text-left font-medium">Date</th>
                      <th className="px-4 py-2 text-right font-medium text-green-500">Appropriate</th>
                      <th className="px-4 py-2 text-right font-medium text-red-500">Inappropriate</th>
                      <th className="px-4 py-2 text-right font-medium">Total</th>
                      <th className="px-4 py-2 text-right font-medium text-blue-400">Accuracy</th>
                      <th className="px-4 py-2 text-right font-medium w-32"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-800">
                    {daily.map(d => (
                      <tr key={d.date} className="hover:bg-gray-800/50 transition-colors">
                        <td className="px-4 py-2.5 text-gray-300 font-mono text-xs">{d.date}</td>
                        <td className="px-4 py-2.5 text-right text-green-400 font-medium">{d.appropriate}</td>
                        <td className="px-4 py-2.5 text-right text-red-400 font-medium">{d.inappropriate}</td>
                        <td className="px-4 py-2.5 text-right text-gray-400">{d.total}</td>
                        <td className="px-4 py-2.5 text-right font-bold text-blue-400">{d.accuracy}%</td>
                        <td className="px-4 py-2.5">
                          {/* Mini bar showing accuracy */}
                          <div className="flex items-center gap-1">
                            <div className="flex-1 h-1.5 bg-gray-700 rounded-full overflow-hidden">
                              <div
                                className="h-full bg-blue-500 rounded-full transition-all"
                                style={{ width: `${d.accuracy}%` }}
                              />
                            </div>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )
        })()}

        {/* Expandable lists */}
        {feedback && (
          <div className="space-y-2">
            {['appropriate', 'inappropriate'].map(label => {
              const rows = feedback.rows?.[label] ?? []
              const isOpen = expanded[label]
              const color = label === 'appropriate' ? 'text-green-400' : 'text-red-400'
              const dotColor = label === 'appropriate' ? 'bg-green-500' : 'bg-red-500'
              return (
                <div key={label} className="bg-gray-900 rounded-lg overflow-hidden">
                  <button
                    onClick={() => toggleExpanded(label)}
                    className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-800 transition-colors"
                  >
                    <span className={`text-sm font-medium capitalize ${color}`}>
                      {label} <span className="text-gray-400 font-normal">({rows.length})</span>
                    </span>
                    {isOpen
                      ? <ChevronDownIcon className="w-4 h-4 text-gray-400" />
                      : <ChevronRightIcon className="w-4 h-4 text-gray-400" />}
                  </button>

                  {isOpen && (
                    <div className="border-t border-gray-700 divide-y divide-gray-800 max-h-96 overflow-y-auto">
                      {rows.length === 0 ? (
                        <p className="px-4 py-3 text-xs text-gray-500">No messages yet.</p>
                      ) : rows.map((r, i) => (
                        <div key={i} className="px-4 py-3 space-y-1">
                          <div className="flex items-center gap-2">
                            <span className={`w-2 h-2 rounded-full shrink-0 ${dotColor}`} />
                            <span className="text-xs text-gray-300 font-medium truncate">{r.message}</span>
                            <span className="text-xs text-gray-600 ml-auto shrink-0">
                              {new Date(r.time).toLocaleDateString()}
                            </span>
                          </div>
                          {r.quoted_text && (
                            <p className="text-xs text-gray-500 pl-4 line-clamp-3 whitespace-pre-line">
                              {r.quoted_text.trim().split('\n')[0]}
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
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
