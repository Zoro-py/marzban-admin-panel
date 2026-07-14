import { cn, formatBytes } from '@/lib/utils'

interface UsageBarProps {
  used: number
  limit: number | null
  className?: string
  compact?: boolean
}

function toneFor(pct: number | null): { bar: string; text: string } {
  if (pct === null) return { bar: 'bg-primary', text: 'text-muted-foreground' }
  if (pct >= 100) return { bar: 'bg-destructive', text: 'text-destructive' }
  if (pct >= 80) return { bar: 'bg-warning', text: 'text-warning' }
  return { bar: 'bg-success', text: 'text-muted-foreground' }
}

/** A compact usage progress bar — used everywhere an account's data
 * consumption shows up (tables, detail pages) instead of a bare "5.2GB /
 * 50GB" text pair that gave no sense of how close to the limit anything was. */
export function UsageBar({ used, limit, className, compact = false }: UsageBarProps) {
  const pct = limit ? Math.min(100, (used / limit) * 100) : null
  const tone = toneFor(pct)

  return (
    <div className={cn('flex min-w-[7rem] flex-col gap-1', className)}>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="font-mono tabular-nums text-foreground">{formatBytes(used)}</span>
        <span className={cn('font-mono tabular-nums', tone.text)}>
          {limit ? `${formatBytes(limit)}` : '∞'}
          {pct !== null && !compact ? ` · ${pct.toFixed(0)}%` : ''}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full transition-all', tone.bar)}
          style={{ width: pct === null ? '8%' : `${Math.max(2, pct)}%` }}
        />
      </div>
    </div>
  )
}
