import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Ban,
  CalendarX,
  CheckCircle2,
  Clock,
  RefreshCw,
  Tag,
  UserX,
  Wallet,
} from 'lucide-react'
import { reportsApi } from '@/lib/api'
import { StatCard } from '@/components/StatCard'
import { Money } from '@/components/Money'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { cn, formatToman } from '@/lib/utils'

/** The dashboard is a WORK QUEUE, not a gallery: one prioritized list of
 * everything that needs the operator's hand today, ordered by severity —
 * not seven equal boxes that are usually empty. */
export function DashboardPage() {
  const { data, isLoading } = useQuery({ queryKey: ['reports', 'summary'], queryFn: reportsApi.summary })
  const financeQuery = useQuery({ queryKey: ['reports', 'finance'], queryFn: reportsApi.finance })
  const openAccount = useOpenAccountInspector()

  if (isLoading || !data) {
    return <p className="text-xs text-muted-foreground">Loading…</p>
  }

  const fin = financeQuery.data
  const attentionCount =
    data.expired_accounts.length +
    data.exhausted_accounts.length +
    data.overdue_customers.length +
    data.near_expiry_accounts.length +
    data.near_quota_accounts.length +
    data.groups_due_for_settlement.length +
    data.unassigned_accounts.length +
    data.no_rate_accounts.length

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Dashboard</h1>
        <p className="text-xs text-muted-foreground">
          {data.total_accounts} accounts across {data.total_customers} customers.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Link to="/finance" className="contents">
          <StatCard
            label="Outstanding (owed to you)"
            value={fin ? formatToman(fin.total_outstanding) : '…'}
            tone={fin && fin.total_outstanding > 0 ? 'destructive' : 'success'}
          />
        </Link>
        <Link to="/finance" className="contents">
          <StatCard
            label="Credit owed back"
            value={fin ? formatToman(fin.total_credit_balance) : '…'}
            tone={fin && fin.total_credit_balance > 0 ? 'credit' : 'default'}
          />
        </Link>
        <Link to="/finance" className="contents">
          <StatCard label="Collected this month" value={fin ? formatToman(fin.revenue_this_month) : '…'} tone="success" />
        </Link>
        <Link to="/finance" className="contents">
          <StatCard label="Charged this month" value={fin ? formatToman(fin.charged_this_month) : '…'} />
        </Link>
      </div>

      <div className="rounded-lg border border-border bg-card">
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <h2 className="text-[13px] font-semibold">Needs attention</h2>
          <span className="text-xs tabular-nums text-muted-foreground">{attentionCount} items</span>
        </div>

        {attentionCount === 0 ? (
          <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
            <CheckCircle2 className="h-6 w-6 text-success" />
            <p className="text-[13px] font-medium">All clear</p>
            <p className="text-xs text-muted-foreground">
              Nothing expired, nothing out of quota, nobody in debt, no cycle overdue.
            </p>
          </div>
        ) : (
          <div className="flex flex-col">
            <QueueSection
              icon={CalendarX}
              tone="danger"
              title="Already expired"
              count={data.expired_accounts.length}
            >
              {data.expired_accounts.map((a) => (
                <AccountQueueRow
                  key={a.account_id}
                  onClick={() => openAccount(a.account_id)}
                  username={a.marzban_username}
                  owner={a.owner_name}
                  metric={<span className="font-medium text-destructive">{Math.abs(a.days_left).toFixed(0)}d ago</span>}
                />
              ))}
            </QueueSection>

            <QueueSection icon={Ban} tone="danger" title="Out of quota" count={data.exhausted_accounts.length}>
              {data.exhausted_accounts.map((a) => (
                <AccountQueueRow
                  key={a.account_id}
                  onClick={() => openAccount(a.account_id)}
                  username={a.marzban_username}
                  owner={a.owner_name}
                  metric={<span className="font-medium text-destructive">{a.used_pct}%</span>}
                />
              ))}
            </QueueSection>

            <QueueSection icon={Wallet} tone="danger" title="Customers in debt" count={data.overdue_customers.length}>
              {data.overdue_customers.map((c) => (
                <Link
                  key={c.customer_id}
                  to={`/customers/${c.customer_id}`}
                  className="flex items-center justify-between gap-3 px-4 py-1.5 text-[13px] hover:bg-muted/50"
                >
                  <span className="truncate">{c.name}</span>
                  <Money amount={c.balance} className="text-xs" />
                </Link>
              ))}
            </QueueSection>

            <QueueSection icon={Clock} tone="warn" title="Expiring within 3 days" count={data.near_expiry_accounts.length}>
              {data.near_expiry_accounts.map((a) => (
                <AccountQueueRow
                  key={a.account_id}
                  onClick={() => openAccount(a.account_id)}
                  username={a.marzban_username}
                  owner={a.owner_name}
                  metric={<span className="font-medium text-warning">{a.days_left}d left</span>}
                />
              ))}
            </QueueSection>

            <QueueSection icon={AlertTriangle} tone="warn" title="Near quota (≥80%)" count={data.near_quota_accounts.length}>
              {data.near_quota_accounts.map((a) => (
                <AccountQueueRow
                  key={a.account_id}
                  onClick={() => openAccount(a.account_id)}
                  username={a.marzban_username}
                  owner={a.owner_name}
                  metric={<span className="font-medium text-warning">{a.used_pct}%</span>}
                />
              ))}
            </QueueSection>

            <QueueSection
              icon={RefreshCw}
              tone="warn"
              title="Groups due for settlement"
              count={data.groups_due_for_settlement.length}
            >
              {data.groups_due_for_settlement.map((g) => (
                <Link
                  key={g.group_id}
                  to={`/groups/${g.group_id}`}
                  className="flex items-center justify-between gap-3 px-4 py-1.5 text-[13px] hover:bg-muted/50"
                >
                  <span className="truncate">{g.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {g.days_overdue}d overdue
                    {g.pending_amount > 0 && (
                      <>
                        {' · '}
                        <span className="font-medium text-warning">{formatToman(g.pending_amount)} pending</span>
                      </>
                    )}
                  </span>
                </Link>
              ))}
            </QueueSection>

            <QueueSection icon={UserX} tone="info" title="Unassigned accounts" count={data.unassigned_accounts.length}>
              {data.unassigned_accounts.map((a) => (
                <AccountQueueRow
                  key={a.account_id}
                  onClick={() => openAccount(a.account_id)}
                  username={a.marzban_username}
                  owner={null}
                  metric={<span className="text-xs text-muted-foreground">assign a customer</span>}
                />
              ))}
            </QueueSection>

            <QueueSection icon={Tag} tone="info" title="No rate configured" count={data.no_rate_accounts.length}>
              {data.no_rate_accounts.map((a) => (
                <AccountQueueRow
                  key={a.account_id}
                  onClick={() => openAccount(a.account_id)}
                  username={a.marzban_username}
                  owner={a.owner_name}
                  metric={<span className="text-xs text-muted-foreground">would bill 0 T</span>}
                />
              ))}
            </QueueSection>
          </div>
        )}
      </div>
    </div>
  )
}

const SECTION_TONES = {
  danger: 'text-destructive',
  warn: 'text-warning',
  info: 'text-muted-foreground',
}

function QueueSection({
  icon: Icon,
  tone,
  title,
  count,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>
  tone: keyof typeof SECTION_TONES
  title: string
  count: number
  children: React.ReactNode
}) {
  if (count === 0) return null
  return (
    <section className="border-b border-border last:border-0">
      <div className="flex items-center gap-2 bg-muted/40 px-4 py-1.5">
        <Icon className={cn('h-3.5 w-3.5', SECTION_TONES[tone])} />
        <h3 className="text-xs font-medium">{title}</h3>
        <span className="text-[11px] tabular-nums text-muted-foreground">{count}</span>
      </div>
      <div className="flex flex-col py-1">{children}</div>
    </section>
  )
}

function AccountQueueRow({
  username,
  owner,
  metric,
  onClick,
}: {
  username: string
  owner: string | null
  metric: React.ReactNode
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center justify-between gap-3 px-4 py-1.5 text-left text-[13px] hover:bg-muted/50"
    >
      <span className="flex min-w-0 items-baseline gap-2">
        <span className="truncate font-mono text-xs font-medium">{username}</span>
        {owner && <span className="truncate text-[11px] text-muted-foreground">{owner}</span>}
      </span>
      <span className="shrink-0 text-xs tabular-nums">{metric}</span>
    </button>
  )
}
