import { useState, useRef, useEffect } from 'react'
import { PaperAirplaneIcon, ArrowTopRightOnSquareIcon, CheckIcon, PaperClipIcon, XMarkIcon } from '@heroicons/react/24/outline'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { sendChat, fileAnswer } from '../api'
import CostBadge from '../components/CostBadge'
import { useAppState } from '../AppStateContext'

const DEFAULT_VAULT = import.meta.env.VITE_VAULT_NAME || 'llm_wiki'
const WIKI_LINK_PREFIX = 'obsidian-wiki://'

function preprocessWikiLinks(text, vaultName) {
  return text.replace(/\[\[(.+?)\]\]/g, (_, title) => {
    const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
    const obsidianUrl = `obsidian://open?vault=${vaultName}&file=wiki/entities/${slug}`
    return `[${title}](${WIKI_LINK_PREFIX}${encodeURIComponent(obsidianUrl)})`
  })
}

/** Custom component map for react-markdown */
const mdComponents = {
  // Wiki links rendered as purple badges
  a({ href, children }) {
    if (href?.startsWith(WIKI_LINK_PREFIX)) {
      const obsidianUrl = decodeURIComponent(href.slice(WIKI_LINK_PREFIX.length))
      return (
        <a
          href={obsidianUrl}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 px-2 py-0.5 mx-0.5 bg-accent/20 border border-accent/40 rounded text-accent text-xs font-mono hover:bg-accent/30 transition-colors"
        >
          {children}
          <ArrowTopRightOnSquareIcon className="w-3 h-3 inline-block" />
        </a>
      )
    }
    return (
      <a href={href} target="_blank" rel="noreferrer"
        className="text-accent underline underline-offset-2 hover:text-accent/80">
        {children}
      </a>
    )
  },
  p({ children })         { return <p className="mb-3 last:mb-0 leading-relaxed">{children}</p> },
  ul({ children })        { return <ul className="list-disc list-outside ml-5 mb-3 space-y-1">{children}</ul> },
  ol({ children })        { return <ol className="list-decimal list-outside ml-5 mb-3 space-y-1">{children}</ol> },
  li({ children })        { return <li className="leading-relaxed">{children}</li> },
  strong({ children })    { return <strong className="font-semibold text-white">{children}</strong> },
  em({ children })        { return <em className="italic text-white/75">{children}</em> },
  h1({ children })        { return <h1 className="text-base font-bold text-white mt-4 mb-2">{children}</h1> },
  h2({ children })        { return <h2 className="text-sm font-bold text-white mt-3 mb-1.5">{children}</h2> },
  h3({ children })        { return <h3 className="text-sm font-semibold text-white/90 mt-2 mb-1">{children}</h3> },
  code({ inline, children }) {
    return inline
      ? <code className="px-1 py-0.5 bg-ink-900 rounded text-xs font-mono text-accent/80">{children}</code>
      : <pre className="p-3 my-2 bg-ink-900 rounded-lg text-xs font-mono text-white/80 overflow-x-auto"><code>{children}</code></pre>
  },
  blockquote({ children }) {
    return <blockquote className="border-l-2 border-accent/40 pl-3 italic text-white/60 mb-3">{children}</blockquote>
  },
  table({ children })     { return <div className="overflow-x-auto mb-3"><table className="text-xs border-collapse w-full">{children}</table></div> },
  thead({ children })     { return <thead className="bg-ink-800">{children}</thead> },
  th({ children })        { return <th className="border border-border px-3 py-1.5 text-left font-semibold text-white">{children}</th> },
  td({ children })        { return <td className="border border-border px-3 py-1.5 text-white/80">{children}</td> },
}

function AnswerBody({ text, vaultName }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
      {preprocessWikiLinks(text, vaultName)}
    </ReactMarkdown>
  )
}

const CDS_SECTIONS = [
  { key: 'immediate_actions',          label: 'Immediate Actions',          color: 'text-red-400',    border: 'border-red-800/50',  bg: 'bg-red-950/20' },
  { key: 'clinical_reasoning',         label: 'Clinical Reasoning',         color: 'text-amber-400',  border: 'border-amber-800/50',bg: 'bg-amber-950/20' },
  { key: 'monitoring_followup',        label: 'Monitoring & Follow-up',     color: 'text-blue-400',   border: 'border-blue-800/50', bg: 'bg-blue-950/20' },
  { key: 'alternative_considerations', label: 'Alternative Considerations', color: 'text-purple-400', border: 'border-purple-800/50',bg: 'bg-purple-950/20' },
]

function CdsBody({ msg, vaultName }) {
  return (
    <div className="space-y-3">
      {CDS_SECTIONS.map(({ key, label, color, border, bg }) => {
        const items = msg[key] || []
        if (!items.length) return null
        return (
          <div key={key} className={`rounded-lg border ${border} ${bg} px-3 py-2.5`}>
            <p className={`text-[10px] uppercase tracking-widest font-semibold ${color} mb-2`}>{label}</p>
            <ul className="space-y-1.5">
              {items.map((item, i) => (
                <li key={i} className="flex gap-2 text-sm text-white/85 leading-snug">
                  <span className={`${color} mt-0.5 flex-shrink-0`}>›</span>
                  <span>
                    <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                      {preprocessWikiLinks(item, vaultName)}
                    </ReactMarkdown>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )
      })}
    </div>
  )
}

function Message({ msg, vaultName }) {
  const { activeKB } = useAppState()
  const [filed, setFiled] = useState(false)
  const [filing, setFiling] = useState(false)

  const handleFile = async () => {
    setFiling(true)
    try {
      await fileAnswer(msg.question, msg.answer, activeKB)
      setFiled(true)
    } catch (e) {
      console.error(e)
    } finally {
      setFiling(false)
    }
  }

  if (msg.role === 'user') {
    return (
      <div className="flex justify-end mb-4">
        <div className="max-w-xl flex flex-col items-end gap-2">
          {msg.images?.length > 0 && (
            <div className="flex flex-wrap gap-2 justify-end">
              {msg.images.map((img, i) => (
                <img key={i} src={img.dataUrl} alt="" className="max-h-40 max-w-xs rounded-lg border border-white/10 object-cover" />
              ))}
            </div>
          )}
          {msg.content && (
            <div className="px-4 py-3 bg-accent rounded-2xl rounded-tr-sm text-sm text-white leading-relaxed">
              {msg.content}
            </div>
          )}
        </div>
      </div>
    )
  }

  const isCds = msg.mode === 'cds'

  return (
    <div className="flex justify-start mb-4">
      <div className="max-w-2xl w-full">
        <div className="px-5 py-4 bg-surface border-l-2 border-accent rounded-2xl rounded-tl-sm text-sm text-white/90">
          {isCds
            ? <CdsBody msg={msg} vaultName={vaultName} />
            : <AnswerBody text={msg.answer} vaultName={vaultName} />
          }
        </div>
        <div className="flex items-center gap-4 mt-2 px-1">
          <CostBadge
            inputTokens={msg.input_tokens}
            outputTokens={msg.output_tokens}
            costUsd={msg.cost_usd}
            model={msg.model}
          />
          {msg.gap_registered && (
            <span className="flex items-center gap-1.5 text-xs text-amber-500/70 font-mono" title={msg.gap_sections?.join(', ')}>
              <span>&#9651;</span>
              <span>gap registered:</span>
              <span className="text-amber-400">{msg.gap_entity || msg.gap_registered}</span>
              {msg.gap_sections?.length > 0 && (
                <span className="text-amber-500/50">({msg.gap_sections.join(', ')})</span>
              )}
            </span>
          )}
          {!isCds && (
            <button
              onClick={handleFile}
              disabled={filed || filing}
              className="ml-auto flex items-center gap-1 text-xs text-muted hover:text-accent transition-colors disabled:opacity-50"
            >
              {filed ? (
                <>
                  <CheckIcon className="w-3 h-3 text-success" />
                  <span className="text-success">Filed</span>
                </>
              ) : (
                <>
                  <ArrowTopRightOnSquareIcon className="w-3 h-3" />
                  {filing ? 'Filing...' : 'File this answer ↗'}
                </>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default function Chat() {
  const { chat, setChat, activeKB } = useAppState()
  const { messages, input } = chat
  const setMessages = fn => setChat(prev => ({ ...prev, messages: typeof fn === 'function' ? fn(prev.messages) : fn }))
  const setInput    = val => setChat(prev => ({ ...prev, input: val }))
  const vaultName   = activeKB === 'default' ? DEFAULT_VAULT : activeKB

  const [loading, setLoading] = useState(false)
  const [mode, setMode] = useState('qna')
  const [images, setImages] = useState([])
  const bottomRef = useRef()
  const textareaRef = useRef()
  const fileInputRef = useRef()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const addImageFile = (file) => {
    if (!file.type.startsWith('image/')) return
    const reader = new FileReader()
    reader.onload = (e) => {
      const dataUrl = e.target.result
      const base64 = dataUrl.split(',')[1]
      setImages(prev => [...prev, { dataUrl, data: base64, media_type: file.type }])
    }
    reader.readAsDataURL(file)
  }

  const handlePaste = (e) => {
    for (const item of e.clipboardData.items) {
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        addImageFile(item.getAsFile())
      }
    }
  }

  const handleSend = async () => {
    const q = input.trim()
    if ((!q && images.length === 0) || loading) return
    const sentImages = images
    setInput('')
    setImages([])
    setMessages(prev => [...prev, { role: 'user', content: q, images: sentImages }])
    setLoading(true)

    const apiImages = sentImages.map(({ data, media_type }) => ({ data, media_type }))
    try {
      const data = await sendChat(q, activeKB, apiImages, mode)
      setMessages(prev => [...prev, {
        role: 'assistant',
        question: q,
        mode: data.mode,
        answer: data.answer,
        immediate_actions: data.immediate_actions,
        clinical_reasoning: data.clinical_reasoning,
        monitoring_followup: data.monitoring_followup,
        alternative_considerations: data.alternative_considerations,
        input_tokens: data.input_tokens,
        output_tokens: data.output_tokens,
        cost_usd: data.cost_usd,
        model: data.model,
        pages_consulted: data.pages_consulted,
        gap_registered: data.gap_registered,
        gap_entity: data.gap_entity,
        gap_sections: data.gap_sections,
      }])
    } catch (e) {
      setMessages(prev => [...prev, {
        role: 'assistant',
        question: q,
        answer: `Error: ${e.message}`,
        cost_usd: 0,
      }])
    } finally {
      setLoading(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const removeImage = (i) => setImages(prev => prev.filter((_, idx) => idx !== i))

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-8 py-5 border-b border-border flex-shrink-0 flex items-center justify-between">
        <div>
          <h1 className="font-display text-2xl font-semibold text-white">Chat</h1>
          <p className="text-sm text-muted mt-0.5">
            {mode === 'cds' ? 'Clinical Decision Support — structured actions & reasoning' : 'Ask questions about your wiki'}
          </p>
        </div>
        <div className="flex items-center gap-1 bg-ink-800 border border-border rounded-lg p-1">
          <button
            onClick={() => setMode('qna')}
            className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
              mode === 'qna' ? 'bg-accent text-white' : 'text-muted hover:text-white'
            }`}
          >
            QnA
          </button>
          <button
            onClick={() => setMode('cds')}
            className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
              mode === 'cds' ? 'bg-accent text-white' : 'text-muted hover:text-white'
            }`}
          >
            CDS
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-8 py-6">
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center">
            <div className="w-16 h-16 rounded-full bg-ink-800 flex items-center justify-center mb-4">
              <span className="font-display text-2xl text-accent">?</span>
            </div>
            <p className="text-white text-base mb-2">Ask anything about your wiki</p>
            <p className="text-sm text-muted max-w-sm">
              Claude will search relevant wiki pages and synthesize a cited answer.
              Wiki links open directly in Obsidian.
            </p>
          </div>
        ) : (
          messages.map((msg, i) => <Message key={i} msg={msg} vaultName={vaultName} />)
        )}
        {loading && (
          <div className="flex justify-start mb-4">
            <div className="px-5 py-4 bg-surface border-l-2 border-accent rounded-2xl rounded-tl-sm">
              <div className="flex gap-1.5">
                <span className="w-2 h-2 bg-accent/60 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                <span className="w-2 h-2 bg-accent/60 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                <span className="w-2 h-2 bg-accent/60 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
              </div>
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="px-8 py-5 border-t border-border flex-shrink-0">
        {images.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-3">
            {images.map((img, i) => (
              <div key={i} className="relative group">
                <img src={img.dataUrl} alt="" className="h-16 w-16 object-cover rounded-lg border border-border" />
                <button
                  onClick={() => removeImage(i)}
                  className="absolute -top-1.5 -right-1.5 w-4 h-4 bg-ink-900 border border-border rounded-full flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
                >
                  <XMarkIcon className="w-2.5 h-2.5 text-muted" />
                </button>
              </div>
            ))}
          </div>
        )}
        <div className="flex gap-3 items-end">
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            multiple
            className="hidden"
            onChange={e => { Array.from(e.target.files).forEach(addImageFile); e.target.value = '' }}
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={loading}
            className="p-3 bg-ink-800 border border-border hover:border-accent disabled:opacity-40 text-muted hover:text-accent rounded-xl transition-colors flex-shrink-0"
            title="Attach image"
          >
            <PaperClipIcon className="w-5 h-5" />
          </button>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            disabled={loading}
            placeholder="Ask a question... (Enter to send, Shift+Enter for newline)"
            rows={1}
            className="flex-1 px-4 py-3 bg-ink-800 border border-border rounded-xl text-sm text-white placeholder:text-muted focus:outline-none focus:border-accent transition-colors resize-none disabled:opacity-50"
            style={{ maxHeight: '100px', overflowY: 'auto' }}
          />
          <button
            onClick={handleSend}
            disabled={(!input.trim() && images.length === 0) || loading}
            className="p-3 bg-accent hover:bg-accent-dim disabled:opacity-40 disabled:cursor-not-allowed text-white rounded-xl transition-colors flex-shrink-0"
          >
            <PaperAirplaneIcon className="w-5 h-5" />
          </button>
        </div>
      </div>
    </div>
  )
}
