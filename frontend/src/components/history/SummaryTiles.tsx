import { Badge } from '@/components/ui/badge'
import type { ChargeHistory } from '@/lib/types'
import { cn, formatAgo, formatDate, formatToman } from '@/lib/utils'

function fmtGb(gb: number): string {
  return String(Number(gb.toFixed(2)))
}

/** One card per selected account (plus a selection-wide first card): the
 * window's headline numbers, each labeled for what it actually is. GB is
 * honest about coverage — "recorded on 3 of 7 charges" — because most legacy
 * charge rows carry no GB figure and a bare total would misrepresent them. */
export function SummaryTiles({ data }: { data: ChargeHistory }) {
  const t = data.totals
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className="rounded-lg border border-primary/25 bg-primary/5 px-4 py-3">
        <p className="truncate text-xs text-muted-foreground">Selection total · {data.accounts.length} accounts</p>
        <p className="mt-1 text-lg font-semibold leading-tight tabular-nums">{formatToman(t.charged_amount)}</p>
        <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
          {t.charge_count} charges
          {t.credit_count > 0 && t.credited_amount != null && ` · ${t.credit_count} payments ${formatToman(t.credited_amount)}`}
        </p>
        {t.charged_gb_known != null && (
          <p className="text-[11px] leading-snug text-muted-foreground">
            {fmtGb(t.charged_gb_known)} GB recorded on {t.charged_gb_known_count} of {t.charge_count} charges
          </p>
        )}
      </div>

      {data.accounts.map((a) => {
        const s = data.summaries[String(a.id)]
        if (!s) return null
        return (
          <div key={a.id} className={cn('rounded-lg border border-border bg-card px-4 py-3', a.deleted && 'opacity-75')}>
            <p className="flex items-center gap-1.5 truncate text-xs text-muted-foreground">
              <span className="truncate font-mono text-[12px] text-foreground">{a.username}</span>
              {a.deleted && (
                <Badge variant="warning" className="shrink-0">
                  deleted
                </Badge>
              )}
            </p>
            <p className="mt-1 text-lg font-semibold leading-tight tabular-nums">{formatToman(s.charged_amount)}</p>
            <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
              {s.charge_count} {s.charge_count === 1 ? 'charge' : 'charges'}
              {s.credit_count > 0 && s.credited_amount != null && ` · ${s.credit_count} payments ${formatToman(s.credited_amount)}`}
            </p>
            <p className="text-[11px] leading-snug text-muted-foreground">
              {s.last_charge_at ? (
                <>last {formatAgo(s.last_charge_at)} · {formatDate(s.last_charge_at)}</>
              ) : (
                'no charges in this period'
              )}
              {s.avg_days_between_charges != null && <> · ≈every {s.avg_days_between_charges}d</>}
            </p>
            <p className="text-[11px] leading-snug text-muted-foreground">
              {s.charged_gb_known != null ? (
                <>
                  {fmtGb(s.charged_gb_known)} GB{' '}
                  <span className="text-muted-foreground/80">
                    (recorded on {s.charged_gb_known_count} of {s.charge_count})
                  </span>
                </>
              ) : s.charge_count > 0 ? (
                'GB not recorded on these charges'
              ) : (
                ''
              )}
            </p>
          </div>
        )
      })}
    </div>
  )
}
