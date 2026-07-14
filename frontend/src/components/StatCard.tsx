import { cn } from '@/lib/utils'

interface StatCardProps {
  label: string
  value: string | number
  tone?: 'default' | 'warning' | 'destructive' | 'success' | 'credit'
  /** A second, smaller related figure ("2 exhausted" beside "5 near quota") —
   * related-but-distinct counts never get silently merged into one number. */
  secondary?: { label: string; value: string | number; tone?: StatCardProps['tone'] }
  className?: string
}

const TONE_TEXT: Record<NonNullable<StatCardProps['tone']>, string> = {
  default: 'text-foreground',
  warning: 'text-warning',
  destructive: 'text-destructive',
  success: 'text-success',
  credit: 'text-credit',
}

/** Quiet stat tile: label over value, color only when the value itself is a
 * state (debt red, all-clear green). No icon boxes — decoration competed with
 * the numbers. Values use proportional figures (not tabular) per large-number
 * typography convention. */
export function StatCard({ label, value, tone = 'default', secondary, className }: StatCardProps) {
  return (
    <div className={cn('rounded-lg border border-border bg-card px-4 py-3', className)}>
      <p className="truncate text-xs text-muted-foreground">{label}</p>
      <div className="mt-1 flex items-baseline gap-2">
        <p className={cn('text-lg font-semibold leading-tight', TONE_TEXT[tone])}>{value}</p>
        {secondary && (
          <span className={cn('text-xs font-medium', TONE_TEXT[secondary.tone ?? 'default'])}>
            {secondary.value} {secondary.label}
          </span>
        )}
      </div>
    </div>
  )
}
