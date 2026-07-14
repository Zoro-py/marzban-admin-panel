import { useQuery } from '@tanstack/react-query'
import { reportsApi } from '@/lib/api'
import { StatCard } from '@/components/StatCard'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { RevenueChart } from '@/components/finance/RevenueChart'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { cn, formatDate, formatToman } from '@/lib/utils'

export function FinancePage() {
  const { data, isLoading } = useQuery({ queryKey: ['reports', 'finance'], queryFn: reportsApi.finance })
  const openAccount = useOpenAccountInspector()

  if (isLoading || !data) {
    return <p className="text-xs text-muted-foreground">Loading…</p>
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Finance</h1>
        <p className="text-xs text-muted-foreground">Balances, money flow, and every effective rate in one place.</p>
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
        <h2 className="mb-2 text-[13px] font-semibold">Money flow — last 30 days</h2>
        <RevenueChart collected={data.revenue_by_day} charged={data.charged_by_day} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <div className="overflow-hidden rounded-lg border border-border bg-card">
          <div className="border-b border-border px-4 py-2.5">
            <h2 className="text-[13px] font-semibold">Recent transactions</h2>
          </div>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead>Who</TableHead>
                <TableHead>Type</TableHead>
                <TableHead className="text-right">Amount</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.recent_transactions.length === 0 && (
                <TableRow>
                  <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                    No transactions yet.
                  </TableCell>
                </TableRow>
              )}
              {data.recent_transactions.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="text-xs text-muted-foreground">{formatDate(t.date)}</TableCell>
                  <TableCell className="max-w-[160px] truncate" title={t.customer_name ?? t.group_name ?? undefined}>
                    {t.customer_name ?? t.group_name ?? '—'}
                  </TableCell>
                  <TableCell>
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
                <TableHead>Owner</TableHead>
                <TableHead>Mode</TableHead>
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
                  <TableCell className="max-w-[120px] truncate text-muted-foreground">
                    {r.customer_name ?? r.group_name ?? '—'}
                  </TableCell>
                  <TableCell>
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
