import { useState, useRef, useEffect } from 'react'
import { PaperAirplaneIcon, ArrowTopRightOnSquareIcon, CheckIcon } from '@heroicons/react/24/outline'
import { sendChat, fileAnswer } from '../api'
import CostBadge from '../components/CostBadge'

const VAULT_NAME = import.meta.env.VITE_VAULT_NAME || 'llm_wiki'

function obsidianLink(title) {
  // Convert title to likely file path
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
  return `obsidian://open?vault=${VAULT_NAME}&file=wiki/entities/${slug}`
}

function renderAnswer(text) {
  // Replace [[wiki links]] with clickable badges
  const parts = text.split(/(\[\[.+?\]\])/g)
  return parts.map((part, i) => {
    const match = part.match(/^\[\[(.+?)\]\]$/)
    if (match) {
      const title = match[1]
      return (
        <a
          key={i}
          href={obsidianLink(title)}
          target="_blank"
          rel="noreferrer"
          className="inline-flex items-center gap-1 px-2 py-0.5 mx-0.5 bg-accent/20 border border-accent/40 rounded text-accent text-xs font-mono hover:bg-accent/30 transition-colors"
        >
          {title}
          <ArrowTopRightOnSquareIcon className="w-3 h-3" />
        </a>
      )
    }
    return <span key={i}>{part}</span>
  })
}

function Message({ msg }) {
  const [filed, setFiled] = useState(false)
  const [filing, setFiling] = useState(false)

  const handleFile = async () => {
    setFiling(true)
    try {
      await fileAnswer(msg.question, msg.answer)
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
        <div className="px-5 py-4 bg-surface border-l-2 border-accent rounded-2xl rounded-tl-sm text-sm text-white/90 leading-relaxed whitespace-pre-wrap">
          {renderAnswer(msg.answer)}
        </div>
        <div className="flex items-center gap-4 mt-2 px-1">
          <CostBadge
            inputTokens={msg.input_tokens}
            outputTokens={msg.output_tokens}
            costUsd={msg.cost_usd}
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
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
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
      const data = await sendChat(q)
      setMessages(prev => [...prev, {
        role: 'assistant',
        question: q,
        answer: data.answer,
        input_tokens: data.input_tokens,
        output_tokens: data.output_tokens,
        cost_usd: data.cost_usd,
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
          messages.map((msg, i) => <Message key={i} msg={msg} />)
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
