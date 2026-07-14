import { cn, formatBytes } from '@/lib/utils'

interface UsageBarProps {
  used: number
  limit: number | null
  className?: string
  compact?: boolean
}

/* Meter spec: the FILL carries severity (accent → warning → danger) and the
   unfilled track is a lighter step of the same hue, so state reads across the
   whole bar — and healthy accounts stay quiet (accent, not green confetti). */
function toneFor(pct: number | null): { bar: string; track: string; text: string } {
  if (pct === null) return { bar: 'bg-muted-foreground/30', track: 'bg-muted', text: 'text-muted-foreground' }
  if (pct >= 100) return { bar: 'bg-destructive', track: 'bg-destructive/15', text: 'text-destructive' }
  if (pct >= 80) return { bar: 'bg-warning', track: 'bg-warning/15', text: 'text-warning' }
  return { bar: 'bg-primary/80', track: 'bg-primary/12', text: 'text-muted-foreground' }
}

/** Data consumption meter, used everywhere an account's usage shows up.
 * compact: single line (bar + terse figures) for dense tables;
 * full: figures above the bar, for detail surfaces. */
export function UsageBar({ used, limit, className, compact = false }: UsageBarProps) {
  const pct = limit ? Math.min(100, (used / limit) * 100) : null
  const tone = toneFor(pct)

  if (compact) {
    return (
      <div className={cn('flex items-center gap-2', className)}>
        <div className={cn('h-1 w-16 shrink-0 overflow-hidden rounded-full', tone.track)}>
          <div className={cn('h-full rounded-full', tone.bar)} style={{ width: pct === null ? '100%' : `${Math.max(2, pct)}%` }} />
        </div>
        <span className={cn('whitespace-nowrap font-mono text-[11px] tabular-nums', tone.text)}>
          {pct !== null ? `${pct.toFixed(0)}%` : '∞'}
        </span>
      </div>
    )
  }

  return (
    <div className={cn('flex min-w-[7rem] flex-col gap-1', className)}>
      <div className="flex items-baseline justify-between gap-2 text-xs">
        <span className="font-mono tabular-nums text-foreground">{formatBytes(used)}</span>
        <span className={cn('font-mono tabular-nums', tone.text)}>
          {limit ? formatBytes(limit) : '∞'}
          {pct !== null ? ` · ${pct.toFixed(0)}%` : ''}
        </span>
      </div>
      <div className={cn('h-1.5 w-full overflow-hidden rounded-full', tone.track)}>
        <div className={cn('h-full rounded-full transition-all', tone.bar)} style={{ width: pct === null ? '100%' : `${Math.max(2, pct)}%` }} />
      </div>
    </div>
  )
}
