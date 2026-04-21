import { useState, useEffect } from 'react'
import { getKBPrompt, updateKBPrompt, clearWikiContents } from '../api'
import { useAppState } from '../AppStateContext'

export default function KBSettings() {
  const { activeKB } = useAppState()
  const [content, setContent] = useState('')
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [clearConfirm, setClearConfirm] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [clearResult, setClearResult] = useState(null)

  useEffect(() => {
    setLoading(true)
    setError(null)
    setSaved(false)
    getKBPrompt(activeKB)
      .then(data => { setContent(data.content); setLoading(false) })
      .catch(e => { setError(e.message); setLoading(false) })
  }, [activeKB])

  async function handleSave() {
    setError(null)
    setSaved(false)
    try {
      await updateKBPrompt(activeKB, content)
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
    } catch (e) {
      setError(e.message)
    }
  }

  async function handleClear() {
    setClearing(true)
    setClearResult(null)
    try {
      const data = await clearWikiContents(activeKB)
      setClearResult(`Deleted ${data.deleted} files.`)
      setClearConfirm(false)
      setTimeout(() => setClearResult(null), 4000)
    } catch (e) {
      setClearResult(`Error: ${e.message}`)
    } finally {
      setClearing(false)
    }
  }

  return (
    <div className="p-8 max-w-3xl mx-auto">
      <div className="mb-6">
        <h1 className="text-lg font-semibold text-white mb-1">
          KB Prompt — <span className="text-accent font-mono">{activeKB}</span>
        </h1>
        <p className="text-sm text-muted">
          Edit the CLAUDE.md system prompt for this knowledge base. This controls how
          the AI reads, writes, and reasons about content — persona, depth, style, and rules.
        </p>
      </div>

      {loading && <p className="text-muted text-sm">Loading…</p>}

      {!loading && (
        <>
          <textarea
            value={content}
            onChange={e => setContent(e.target.value)}
            className="w-full h-[60vh] bg-ink-900 border border-border rounded-lg p-4 text-sm text-white font-mono leading-relaxed resize-none focus:outline-none focus:border-accent"
            spellCheck={false}
          />

          <div className="mt-4 flex items-center gap-3">
            <button
              onClick={handleSave}
              className="px-4 py-2 bg-accent text-ink-950 rounded-md text-sm font-medium hover:bg-accent/90 transition-colors"
            >
              Save
            </button>
            {saved && <span className="text-xs text-green-400">Saved</span>}
            {error && <span className="text-xs text-red-400">{error}</span>}
          </div>

          <div className="mt-10 pt-8 border-t border-border">
            <h2 className="text-sm font-semibold text-white mb-1">Danger Zone</h2>
            <p className="text-xs text-muted mb-4">
              Permanently delete all wiki pages in <span className="font-mono text-accent">{activeKB}</span>.
              The system prompt and raw source files are not affected.
            </p>
            {!clearConfirm ? (
              <button
                onClick={() => setClearConfirm(true)}
                className="px-4 py-2 border border-red-800 text-red-400 rounded-md text-sm hover:bg-red-950/40 transition-colors"
              >
                Clear Wiki Contents
              </button>
            ) : (
              <div className="flex items-center gap-3">
                <button
                  onClick={handleClear}
                  disabled={clearing}
                  className="px-4 py-2 bg-red-700 hover:bg-red-600 disabled:opacity-50 text-white rounded-md text-sm font-medium transition-colors"
                >
                  {clearing ? 'Deleting…' : 'Yes, delete everything'}
                </button>
                <button
                  onClick={() => setClearConfirm(false)}
                  className="px-4 py-2 text-muted hover:text-white text-sm transition-colors"
                >
                  Cancel
                </button>
              </div>
            )}
            {clearResult && (
              <p className={`mt-3 text-xs ${clearResult.startsWith('Error') ? 'text-red-400' : 'text-green-400'}`}>
                {clearResult}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
