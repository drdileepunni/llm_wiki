export default function CostBadge({ inputTokens, outputTokens, costUsd, model }) {
  if (!costUsd && costUsd !== 0) return null
  return (
    <div className="flex items-center gap-3 mt-2 text-xs font-mono text-muted flex-wrap">
      {model && (
        <>
          <span className="px-1.5 py-0.5 bg-ink-800 border border-border rounded text-accent/80">
            {model}
          </span>
          <span className="text-border">·</span>
        </>
      )}
      <span>{inputTokens?.toLocaleString()} in</span>
      <span className="text-border">·</span>
      <span>{outputTokens?.toLocaleString()} out</span>
      <span className="text-border">·</span>
      <span className="text-warning">${costUsd.toFixed(4)}</span>
    </div>
  )
}
