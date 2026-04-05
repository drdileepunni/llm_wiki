export default function CostBadge({ inputTokens, outputTokens, costUsd }) {
  if (!costUsd && costUsd !== 0) return null
  return (
    <div className="flex items-center gap-3 mt-2 text-xs font-mono text-muted">
      <span>{inputTokens?.toLocaleString()} in</span>
      <span className="text-border">·</span>
      <span>{outputTokens?.toLocaleString()} out</span>
      <span className="text-border">·</span>
      <span className="text-warning">${costUsd.toFixed(4)}</span>
    </div>
  )
}
