import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Clock, Users, Network, UserX, Wallet } from 'lucide-react'
import { reportsApi } from '@/lib/api'
import { StatCard } from '@/components/StatCard'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { formatToman } from '@/lib/utils'

export function DashboardPage() {
  const { data, isLoading } = useQuery({ queryKey: ['reports', 'summary'], queryFn: reportsApi.summary })

  if (isLoading || !data) {
    return <p className="text-sm text-muted-foreground">Loading…</p>
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <p className="text-sm text-muted-foreground">Live overview across every Marzban account you resell.</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-5">
        <StatCard label="Customers" value={data.total_customers} icon={Users} />
        <StatCard label="Accounts" value={data.total_accounts} icon={Network} />
        <StatCard label="Overdue customers" value={data.overdue_customers.length} icon={Wallet} tone={data.overdue_customers.length ? 'destructive' : 'success'} />
        <StatCard label="Near quota" value={data.near_quota_accounts.length} icon={AlertTriangle} tone={data.near_quota_accounts.length ? 'warning' : 'success'} />
        <StatCard label="Expiring soon" value={data.near_expiry_accounts.length} icon={Clock} tone={data.near_expiry_accounts.length ? 'warning' : 'success'} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
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
                <Badge variant={a.days_left < 0 ? 'destructive' : 'warning'}>
                  {a.days_left < 0 ? 'expired' : `${a.days_left}d left`}
                </Badge>
              </Link>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <UserX className="h-4 w-4 text-muted-foreground" /> Needs assignment
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.unassigned_accounts.length === 0 && <EmptyRow text="Every account has an owner." />}
            {data.unassigned_accounts.map((a) => (
              <Link
                key={a.account_id}
                to={`/accounts?highlight=${a.account_id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span className="font-mono">{a.marzban_username}</span>
                <Badge variant="outline">unassigned</Badge>
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
