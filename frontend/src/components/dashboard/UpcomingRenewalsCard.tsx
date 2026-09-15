import { useQuery } from '@tanstack/react-query'
import { CalendarClock } from 'lucide-react'
import { reportsApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Money } from '@/components/Money'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { cn, formatToman } from '@/lib/utils'

/** Every queued-but-not-yet-activated plan, with what it will actually
 * charge — read-only, changes nothing. Built specifically so "an account
 * gets auto-queued and nobody sees the price until the activation
 * notification" (the exact gap behind a real incident — see
 * docs/DOMAIN_AND_BILLING.md §4.2) has a place to be checked BEFORE that
 * happens, not just after. */
export function UpcomingRenewalsCard() {
  const { data, isLoading } = useQuery({ queryKey: ['reports', 'upcoming-renewals'], queryFn: reportsApi.upcomingRenewals })
  const openAccount = useOpenAccountInspector()

  const rows = data ?? []
  const total = rows.reduce((sum, r) => sum + r.estimated_amount, 0)

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="flex items-center gap-1.5 text-[13px] font-semibold">
          <CalendarClock className="h-3.5 w-3.5 text-muted-foreground" />
          Upcoming renewals
        </h2>
        {rows.length > 0 && (
          <span className="text-xs text-muted-foreground">
            {rows.length} queued · {formatToman(total)} total
          </span>
        )}
      </div>

      {isLoading ? (
        <div className="flex h-20 items-center justify-center text-xs text-muted-foreground">Loading…</div>
      ) : rows.length === 0 ? (
        <div className="flex h-20 items-center justify-center text-xs text-muted-foreground">
          Nothing queued right now.
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Account</TableHead>
              <TableHead className="hidden sm:table-cell">Plan</TableHead>
              <TableHead className="text-right">Est. charge</TableHead>
              <TableHead className="text-right">Activates</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.queued_plan_id} className="cursor-pointer" onClick={() => openAccount(r.account_id)}>
                <TableCell className="max-w-[160px]">
                  <div className="truncate font-mono text-xs font-medium">{r.marzban_username}</div>
                  {r.owner_name && <div className="truncate text-[11px] text-muted-foreground">{r.owner_name}</div>}
                </TableCell>
                <TableCell className="hidden text-xs text-muted-foreground sm:table-cell">
                  {r.data_limit_gb.toLocaleString('en-US')} GB / {r.duration_days}d
                </TableCell>
                <TableCell className="text-right">
                  <Money amount={r.estimated_amount} kind="plain" />
                </TableCell>
                <TableCell className="text-right">
                  <span
                    className={cn(
                      'text-xs tabular-nums',
                      r.days_until_activation !== null && r.days_until_activation <= 1
                        ? 'font-medium text-warning'
                        : 'text-muted-foreground',
                    )}
                  >
                    {r.days_until_activation === null
                      ? '—'
                      : r.days_until_activation <= 0
                        ? 'any moment'
                        : `${r.days_until_activation.toFixed(1)}d`}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}
