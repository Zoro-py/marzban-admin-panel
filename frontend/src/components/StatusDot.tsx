import { cn } from '@/lib/utils'

const STATUS_STYLES: Record<string, { dot: string; label: string }> = {
  active: { dot: 'bg-success', label: 'active' },
  on_hold: { dot: 'bg-warning', label: 'on hold' },
  limited: { dot: 'bg-destructive', label: 'limited' },
  expired: { dot: 'bg-destructive', label: 'expired' },
  disabled: { dot: 'bg-muted-foreground/50', label: 'disabled' },
}

/** Marzban account status as a small dot (+ optional word) instead of a
 * full badge — status appears on every row, so it must be quiet. */
export function StatusDot({ status, showLabel = false, className }: { status: string | null; showLabel?: boolean; className?: string }) {
  const style = (status && STATUS_STYLES[status]) || { dot: 'bg-muted-foreground/40', label: status ?? 'unknown' }
  return (
    <span className={cn('inline-flex items-center gap-1.5', className)} title={showLabel ? undefined : style.label}>
      <span aria-hidden className={cn('h-1.5 w-1.5 shrink-0 rounded-full', style.dot)} />
      {showLabel && <span className="text-xs capitalize text-muted-foreground">{style.label}</span>}
    </span>
  )
}
