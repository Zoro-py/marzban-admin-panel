import type { LucideIcon } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

interface StatCardProps {
  label: string
  value: string | number
  icon: LucideIcon
  tone?: 'default' | 'warning' | 'destructive' | 'success'
  // A second, smaller figure shown beside the primary one — e.g. "3 near quota"
  // next to "1 exhausted" so a related-but-distinct count doesn't need its own
  // full tile (see item 9: exhausted must never be silently folded into the
  // near-quota number, but it also doesn't need equal visual weight).
  secondary?: { label: string; value: string | number; tone?: StatCardProps['tone'] }
}

const TONE_STYLES: Record<NonNullable<StatCardProps['tone']>, string> = {
  default: 'bg-primary/10 text-primary',
  warning: 'bg-warning/15 text-warning',
  destructive: 'bg-destructive/10 text-destructive',
  success: 'bg-success/10 text-success',
}

const TONE_TEXT: Record<NonNullable<StatCardProps['tone']>, string> = {
  default: 'text-primary',
  warning: 'text-warning',
  destructive: 'text-destructive',
  success: 'text-success',
}

export function StatCard({ label, value, icon: Icon, tone = 'default', secondary }: StatCardProps) {
  return (
    <Card>
      <CardContent className="flex items-center gap-4 p-5">
        <div className={cn('flex h-10 w-10 shrink-0 items-center justify-center rounded-lg', TONE_STYLES[tone])}>
          <Icon className="h-5 w-5" />
        </div>
        <div className="min-w-0">
          <p className="truncate text-sm text-muted-foreground">{label}</p>
          <div className="flex items-baseline gap-2">
            <p className="text-xl font-semibold tabular-nums">{value}</p>
            {secondary && (
              <span className={cn('text-xs font-medium tabular-nums', TONE_TEXT[secondary.tone ?? 'default'])}>
                {secondary.value} {secondary.label}
              </span>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
