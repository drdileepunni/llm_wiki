import { useState, useRef, useEffect } from 'react'
import { PaperAirplaneIcon, ArrowTopRightOnSquareIcon, CheckIcon } from '@heroicons/react/24/outline'
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
        <div className="max-w-xl px-4 py-3 bg-accent rounded-2xl rounded-tr-sm text-sm text-white leading-relaxed">
          {msg.content}
        </div>
      </div>
    )
  }

  return (
    <div className="flex justify-start mb-4">
      <div className="max-w-2xl w-full">
        <div className="px-5 py-4 bg-surface border-l-2 border-accent rounded-2xl rounded-tl-sm text-sm text-white/90">
          <AnswerBody text={msg.answer} vaultName={vaultName} />
        </div>
        <div className="flex items-center gap-4 mt-2 px-1">
          <CostBadge
            inputTokens={msg.input_tokens}
            outputTokens={msg.output_tokens}
            costUsd={msg.cost_usd}
            model={msg.model}
          />
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
  const bottomRef = useRef()
  const textareaRef = useRef()

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = async () => {
    const q = input.trim()
    if (!q || loading) return
    setInput('')
    setMessages(prev => [...prev, { role: 'user', content: q }])
    setLoading(true)

    try {
      const data = await sendChat(q, activeKB)
      setMessages(prev => [...prev, {
        role: 'assistant',
        question: q,
        answer: data.answer,
        input_tokens: data.input_tokens,
        output_tokens: data.output_tokens,
        cost_usd: data.cost_usd,
        model: data.model,
        pages_consulted: data.pages_consulted,
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

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-8 py-5 border-b border-border flex-shrink-0">
        <h1 className="font-display text-2xl font-semibold text-white">Chat</h1>
        <p className="text-sm text-muted mt-0.5">Ask questions about your wiki</p>
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
        <div className="flex gap-3 items-end">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={loading}
            placeholder="Ask a question... (Enter to send, Shift+Enter for newline)"
            rows={1}
            className="flex-1 px-4 py-3 bg-ink-800 border border-border rounded-xl text-sm text-white placeholder:text-muted focus:outline-none focus:border-accent transition-colors resize-none disabled:opacity-50"
            style={{ maxHeight: '100px', overflowY: 'auto' }}
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || loading}
            className="p-3 bg-accent hover:bg-accent-dim disabled:opacity-40 disabled:cursor-not-allowed text-white rounded-xl transition-colors flex-shrink-0"
          >
            <PaperAirplaneIcon className="w-5 h-5" />
          </button>
        </div>
      </div>
    </div>
  )
}
