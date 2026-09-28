import * as React from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { BellRing } from 'lucide-react'
import { apiErrorMessage, notificationsApi, reportsApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { StatCard } from '@/components/StatCard'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { RevenueChart } from '@/components/finance/RevenueChart'
import { BalanceLookup } from '@/components/finance/BalanceLookup'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { RangePicker, isRangeValid, resolveRange } from '@/components/history/RangePicker'
import type { RangeCode, RangeValue } from '@/components/history/RangePicker'
import { cn, formatDate, formatToman } from '@/lib/utils'

const FIN_RANGES: RangeCode[] = ['30d', '90d', '6m', 'jm', 'all', 'custom']
// Finance is fully range-driven — the page's default IS the 30-day preset
// (sent explicitly), not a separate unparameterized mode. Same URL language
// as History (r/cs/cu) so the two pickers read identically.
const FIN_DEFAULT: RangeValue = { code: '30d', customSince: '', customUntil: '' }
const YMD = /^\d{4}-\d{2}-\d{2}$/

function parseRange(params: URLSearchParams): RangeValue {
  const code = params.get('r') as RangeCode | null
  if (!code || !FIN_RANGES.includes(code)) return FIN_DEFAULT
  return {
    code,
    customSince: code === 'custom' && YMD.test(params.get('cs') ?? '') ? params.get('cs')! : '',
    customUntil: code === 'custom' && YMD.test(params.get('cu') ?? '') ? params.get('cu')! : '',
  }
}

function buildParams(range: RangeValue): URLSearchParams {
  const params = new URLSearchParams()
  if (range.code !== FIN_DEFAULT.code || range.customSince || range.customUntil) params.set('r', range.code)
  if (range.code === 'custom') {
    if (range.customSince) params.set('cs', range.customSince)
    if (range.customUntil) params.set('cu', range.customUntil)
  }
  return params
}

export function FinancePage() {
  React.useEffect(() => {
    document.title = 'Shiraze | Finance'
  }, [])

  const [searchParams, setSearchParams] = useSearchParams()
  const range = parseRange(searchParams)
  const rangeUsable = isRangeValid(range)
  // Inverted/garbled custom picks degrade to the default window rather than
  // throwing a 422 at the operator — same degrade-don't-crash rule as History.
  const window = rangeUsable ? resolveRange(range) : resolveRange(FIN_DEFAULT)
  const patchRange = (patch: Partial<RangeValue>) => {
    const next = { ...range, ...patch }
    setSearchParams(buildParams(next), { replace: true })
  }

  const { data, isLoading } = useQuery({
    queryKey: ['reports', 'finance', window.since, window.until],
    queryFn: () => reportsApi.finance(window.since, window.until),
  })
  const openAccount = useOpenAccountInspector()

  // The debt nudge normally fires every other day at the configured hour;
  // this is its manual twin (same job, same message), for when the operator
  // wants the reminder out right now. The job reports a Telegram failure as
  // sent=false + error instead of throwing, so all three outcomes land here.
  const nudgeMutation = useMutation({
    mutationFn: notificationsApi.sendDebtNudge,
    onSuccess: (r) => {
      if (r.sent) toast.success(`Debt nudge sent — ${r.count} debtor${r.count === 1 ? '' : 's'}`)
      else if (r.error) toast.error(`Debt nudge failed: ${r.error}`)
      else toast.info('Nothing overdue past 14 days — nothing sent')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  if (isLoading || !data) {
    return <p className="text-xs text-muted-foreground">Loading…</p>
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Finance</h1>
          <p className="text-xs text-muted-foreground">Balances, money flow, and every effective rate in one place.</p>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => nudgeMutation.mutate()}
          disabled={nudgeMutation.isPending}
        >
          <BellRing className="h-3.5 w-3.5" />
          {nudgeMutation.isPending ? 'Sending…' : 'Send debt nudge'}
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="Outstanding (owed to you)"
          value={formatToman(data.total_outstanding)}
          tone={data.total_outstanding > 0 ? 'destructive' : 'success'}
        />
        <StatCard
          label="Credit owed back"
          value={formatToman(data.total_credit_balance)}
          tone={data.total_credit_balance > 0 ? 'credit' : 'default'}
        />
        <StatCard label="Collected this month" value={formatToman(data.revenue_this_month)} tone="success" />
        <StatCard label="Charged this month" value={formatToman(data.charged_this_month)} />
      </div>

      <div className="rounded-lg border border-border bg-card px-4 py-3">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-[13px] font-semibold">Money flow</h2>
          <p className="text-[11px] text-muted-foreground" title={`${window.since} → ${window.until}`}>
            {window.since} → {window.until}
          </p>
        </div>
        <RangePicker value={range} onChange={patchRange} />
        <div className="mt-3">
          <RevenueChart collected={data.revenue_by_day} charged={data.charged_by_day} />
        </div>
      </div>

      <BalanceLookup />

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <div className="border-b border-border px-4 py-2.5">
            <h2 className="text-[13px] font-semibold">Transactions in window</h2>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead>Who</TableHead>
                <TableHead className="hidden sm:table-cell">Type</TableHead>
                <TableHead className="text-right">Amount</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.recent_transactions.length === 0 && (
                <TableRow>
                  <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                    No transactions in this window.
                  </TableCell>
                </TableRow>
              )}
              {data.recent_transactions.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="text-xs text-muted-foreground">{formatDate(t.date)}</TableCell>
                  <TableCell className="max-w-[160px] truncate" title={t.customer_name ?? t.group_name ?? undefined}>
                    {t.customer_name ?? t.group_name ?? '—'}
                  </TableCell>
                  <TableCell className="hidden sm:table-cell">
                    <Badge variant={t.type === 'charge' ? 'destructive' : 'success'}>
                      {t.type === 'charge' ? 'debt' : 'payment'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-right">
                    <span
                      className={cn(
                        'text-xs font-medium tabular-nums',
                        t.type === 'charge' ? 'text-destructive' : 'text-success',
                      )}
                    >
                      {t.type === 'charge' ? '+' : '−'}
                      {Math.round(t.amount).toLocaleString('en-US')} T
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>

        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <div className="border-b border-border px-4 py-2.5">
            <h2 className="text-[13px] font-semibold">Effective rates</h2>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Account</TableHead>
                <TableHead className="hidden sm:table-cell">Owner</TableHead>
                <TableHead className="hidden md:table-cell">Mode</TableHead>
                <TableHead className="text-right">Rate</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.rate_overview.length === 0 && (
                <TableRow>
                  <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                    No accounts yet.
                  </TableCell>
                </TableRow>
              )}
              {data.rate_overview.map((r) => (
                <TableRow key={r.account_id} className="cursor-pointer" onClick={() => openAccount(r.account_id)}>
                  <TableCell className="font-mono text-xs">{r.marzban_username}</TableCell>
                  <TableCell className="hidden max-w-[120px] truncate text-muted-foreground sm:table-cell">
                    {r.customer_name ?? r.group_name ?? '—'}
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <span className="text-xs text-muted-foreground">{r.billing_mode === 'payg' ? 'pay-as-you-go' : 'prepay'}</span>
                  </TableCell>
                  <TableCell className="text-right">
                    {!r.rate_configured ? (
                      <Badge variant="warning">not set</Badge>
                    ) : r.rate_per_gb > 0 ? (
                      <span className="text-xs tabular-nums">
                        {formatToman(r.rate_per_gb)}/GB
                        {r.effective_rate_source === 'account' && r.group_name && (
                          <span className="ml-1 text-[10px] text-muted-foreground">(override)</span>
                        )}
                        {r.effective_rate_source === 'default' && (
                          <span className="ml-1 text-[10px] text-muted-foreground">(default)</span>
                        )}
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">free</span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  )
}
