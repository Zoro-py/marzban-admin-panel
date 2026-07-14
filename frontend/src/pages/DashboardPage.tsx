import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Clock, Users, Network, Wallet, TrendingUp, Ban, CalendarX, Tag } from 'lucide-react'
import { reportsApi } from '@/lib/api'
import { StatCard } from '@/components/StatCard'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { formatToman } from '@/lib/utils'

export function DashboardPage() {
  const { data, isLoading } = useQuery({ queryKey: ['reports', 'summary'], queryFn: reportsApi.summary })
  const financeQuery = useQuery({ queryKey: ['reports', 'finance'], queryFn: reportsApi.finance })

  if (isLoading || !data) {
    return <p className="text-sm text-muted-foreground">Loading…</p>
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">Live overview across every Marzban account you resell.</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-6">
        <StatCard label="Customers" value={data.total_customers} icon={Users} />
        <StatCard label="Accounts" value={data.total_accounts} icon={Network} />
        <Link to="/finance" className="contents">
          <StatCard
            label="Outstanding"
            value={financeQuery.data ? formatToman(financeQuery.data.total_outstanding) : '…'}
            icon={Wallet}
            tone={financeQuery.data && financeQuery.data.total_outstanding > 0 ? 'destructive' : 'success'}
          />
        </Link>
        <Link to="/finance" className="contents">
          <StatCard
            label="Revenue this month"
            value={financeQuery.data ? formatToman(financeQuery.data.revenue_this_month) : '…'}
            icon={TrendingUp}
            tone="success"
          />
        </Link>
        {/* Near-quota and exhausted are deliberately two figures on one tile, never merged
            into one number — a used-up account is a different problem than a soon-to-be-used-up one. */}
        <StatCard
          label="Near quota"
          value={data.near_quota_accounts.length}
          icon={AlertTriangle}
          tone={data.near_quota_accounts.length ? 'warning' : 'success'}
          secondary={
            data.exhausted_accounts.length
              ? { label: 'exhausted', value: data.exhausted_accounts.length, tone: 'destructive' }
              : undefined
          }
        />
        <StatCard
          label="Expiring soon"
          value={data.near_expiry_accounts.length}
          icon={Clock}
          tone={data.near_expiry_accounts.length ? 'warning' : 'success'}
          secondary={
            data.expired_accounts.length
              ? { label: 'expired', value: data.expired_accounts.length, tone: 'destructive' }
              : undefined
          }
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Wallet className="h-4 w-4 text-destructive" /> Overdue customers
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.overdue_customers.length === 0 && <EmptyRow text="Nobody owes anything right now." />}
            {data.overdue_customers.map((c) => (
              <Link
                key={c.customer_id}
                to={`/customers/${c.customer_id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span>{c.name}</span>
                <Badge variant="destructive">{formatToman(c.balance)}</Badge>
              </Link>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Ban className="h-4 w-4 text-destructive" /> Exhausted (out of quota)
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.exhausted_accounts.length === 0 && <EmptyRow text="No accounts have run out of data." />}
            {data.exhausted_accounts.map((a) => (
              <Link
                key={a.account_id}
                to={`/accounts?highlight=${a.account_id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span className="font-mono">{a.marzban_username}</span>
                <Badge variant="destructive">{a.used_pct}%</Badge>
              </Link>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <CalendarX className="h-4 w-4 text-destructive" /> Already expired
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.expired_accounts.length === 0 && <EmptyRow text="Nothing has expired." />}
            {data.expired_accounts.map((a) => (
              <Link
                key={a.account_id}
                to={`/accounts?highlight=${a.account_id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span className="font-mono">{a.marzban_username}</span>
                <Badge variant="destructive">{Math.abs(a.days_left)}d ago</Badge>
              </Link>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <AlertTriangle className="h-4 w-4 text-warning" /> Near quota (≥80%)
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.near_quota_accounts.length === 0 && <EmptyRow text="No accounts near their limit." />}
            {data.near_quota_accounts.map((a) => (
              <Link
                key={a.account_id}
                to={`/accounts?highlight=${a.account_id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span className="font-mono">{a.marzban_username}</span>
                <Badge variant="warning">{a.used_pct}%</Badge>
              </Link>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Clock className="h-4 w-4 text-warning" /> Expiring within 3 days
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.near_expiry_accounts.length === 0 && <EmptyRow text="Nothing expiring soon." />}
            {data.near_expiry_accounts.map((a) => (
              <Link
                key={a.account_id}
                to={`/accounts?highlight=${a.account_id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span className="font-mono">{a.marzban_username}</span>
                <Badge variant="warning">{a.days_left}d left</Badge>
              </Link>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Tag className="h-4 w-4 text-muted-foreground" /> No rate configured
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.no_rate_accounts.length === 0 && (
              <EmptyRow text="Every account resolves to a rate (own, group, or the dashboard default)." />
            )}
            {data.no_rate_accounts.map((a) => (
              <Link
                key={a.account_id}
                to={`/accounts?highlight=${a.account_id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span className="font-mono">{a.marzban_username}</span>
                <Badge variant="outline">would bill ₮0</Badge>
              </Link>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

function EmptyRow({ text }: { text: string }) {
  return <p className="px-2 py-1.5 text-sm text-muted-foreground">{text}</p>
}
