import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Wallet, TrendingUp, TrendingDown, Receipt } from 'lucide-react'
import { reportsApi } from '@/lib/api'
import { StatCard } from '@/components/StatCard'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { RevenueChart } from '@/components/finance/RevenueChart'
import { formatDate, formatToman } from '@/lib/utils'

export function FinancePage() {
  const { data, isLoading } = useQuery({ queryKey: ['reports', 'finance'], queryFn: reportsApi.finance })

  if (isLoading || !data) {
    return <p className="text-sm text-muted-foreground">Loading…</p>
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Finance</h1>
        <p className="text-sm text-muted-foreground">Outstanding balances, revenue, and every rate in one place.</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <StatCard label="Total outstanding" value={formatToman(data.total_outstanding)} icon={Wallet} tone={data.total_outstanding > 0 ? 'destructive' : 'success'} />
        <StatCard label="Credit balance owed back" value={formatToman(data.total_credit_balance)} icon={TrendingDown} tone="default" />
        <StatCard label="Revenue this month" value={formatToman(data.revenue_this_month)} icon={TrendingUp} tone="success" />
        <StatCard label="Charged this month" value={formatToman(data.charged_this_month)} icon={Receipt} tone="warning" />
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Revenue — last 30 days</CardTitle>
        </CardHeader>
        <CardContent>
          <RevenueChart data={data.revenue_by_day} />
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Recent transactions</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
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
                    <TableCell colSpan={4} className="text-center text-muted-foreground">
                      No transactions yet.
                    </TableCell>
                  </TableRow>
                )}
                {data.recent_transactions.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="text-muted-foreground">{formatDate(t.date)}</TableCell>
                    <TableCell>{t.customer_name ?? t.group_name ?? '—'}</TableCell>
                    <TableCell>
                      <Badge variant={t.type === 'charge' ? 'destructive' : 'success'}>{t.type === 'charge' ? 'debt' : 'credit'}</Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{formatToman(t.amount)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Rates</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
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
                    <TableCell colSpan={4} className="text-center text-muted-foreground">
                      No accounts yet.
                    </TableCell>
                  </TableRow>
                )}
                {data.rate_overview.map((r) => (
                  <TableRow key={r.account_id}>
                    <TableCell>
                      <Link to={`/accounts?highlight=${r.account_id}`} className="font-mono text-xs hover:underline">
                        {r.marzban_username}
                      </Link>
                    </TableCell>
                    <TableCell className="text-muted-foreground">{r.customer_name ?? r.group_name ?? '—'}</TableCell>
                    <TableCell>
                      <Badge variant={r.billing_mode === 'payg' ? 'warning' : 'outline'}>{r.billing_mode}</Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {r.rate_per_gb != null ? (
                        <>
                          {formatToman(r.rate_per_gb)}/GB
                          {r.effective_rate_source === 'account' && r.group_name && (
                            <Badge variant="secondary" className="ml-1.5">
                              override
                            </Badge>
                          )}
                        </>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
