import * as React from 'react'
import { Link } from 'react-router-dom'
import { Virtuoso } from 'react-virtuoso'
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
import { OnlineTrendChart } from '@/components/dashboard/OnlineTrendChart'
import { SystemResourcesChart } from '@/components/dashboard/SystemResourcesChart'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { cn, formatToman } from '@/lib/utils'

/** The dashboard is a WORK QUEUE, not a gallery: one prioritized list of
 * everything that needs the operator's hand today, ordered by severity —
 * not seven equal boxes that are usually empty. */
export function DashboardPage() {
  React.useEffect(() => {
    document.title = 'Shiraze | Dashboard'
  }, [])

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
    data.pending_settlement.length +
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

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <StatCard
          label="Pending (not yet charged)"
          value={formatToman(data.total_pending)}
          tone={data.total_pending > 0 ? 'warning' : 'default'}
        />
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

      <OnlineTrendChart />
      <SystemResourcesChart />

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
              Nothing expired, nothing out of quota, nobody in debt, nothing pending settlement.
            </p>
          </div>
        ) : (
          <div className="flex flex-col">
            {/* Capacity/expiry problems first — these need the operator's hand
                to keep service running. Money (debt, pending settlement) is
                real but rarely as time-sensitive, so it's ordered last rather
                than mixed in among "account about to stop working." */}
            <QueueSection
              icon={CalendarX}
              tone="danger"
              title="Already expired"
              count={data.expired_accounts.length}
            >
              {data.expired_accounts.length > 0 && (
                <Virtuoso
                  useWindowScroll
                  data={data.expired_accounts}
                  itemContent={(_, a) => (
                    <AccountQueueRow
                      key={a.account_id}
                      onClick={() => openAccount(a.account_id)}
                      username={a.marzban_username}
                      owner={a.owner_name}
                      hasNextPlan={a.has_next_plan}
                      metric={<span className="font-medium text-destructive">{Math.abs(a.days_left).toFixed(0)}d ago</span>}
                    />
                  )}
                />
              )}
            </QueueSection>

            <QueueSection icon={Ban} tone="danger" title="Out of quota" count={data.exhausted_accounts.length}>
              {data.exhausted_accounts.length > 0 && (
                <Virtuoso
                  useWindowScroll
                  data={data.exhausted_accounts}
                  itemContent={(_, a) => (
                    <AccountQueueRow
                      key={a.account_id}
                      onClick={() => openAccount(a.account_id)}
                      username={a.marzban_username}
                      owner={a.owner_name}
                      hasNextPlan={a.has_next_plan}
                      metric={<span className="font-medium text-destructive">{a.used_pct}%</span>}
                    />
                  )}
                />
              )}
            </QueueSection>

            <QueueSection icon={Clock} tone="warn" title="Expiring within 3 days" count={data.near_expiry_accounts.length}>
              {data.near_expiry_accounts.length > 0 && (
                <Virtuoso
                  useWindowScroll
                  data={data.near_expiry_accounts}
                  itemContent={(_, a) => (
                    <AccountQueueRow
                      key={a.account_id}
                      onClick={() => openAccount(a.account_id)}
                      username={a.marzban_username}
                      owner={a.owner_name}
                      hasNextPlan={a.has_next_plan}
                      metric={<span className="font-medium text-warning">{a.days_left}d left</span>}
                    />
                  )}
                />
              )}
            </QueueSection>

            <QueueSection icon={AlertTriangle} tone="warn" title="Near quota (≥80%)" count={data.near_quota_accounts.length}>
              {data.near_quota_accounts.length > 0 && (
                <Virtuoso
                  useWindowScroll
                  data={data.near_quota_accounts}
                  itemContent={(_, a) => (
                    <AccountQueueRow
                      key={a.account_id}
                      onClick={() => openAccount(a.account_id)}
                      username={a.marzban_username}
                      owner={a.owner_name}
                      hasNextPlan={a.has_next_plan}
                      metric={<span className="font-medium text-warning">{a.used_pct}%</span>}
                    />
                  )}
                />
              )}
            </QueueSection>

            <QueueSection icon={UserX} tone="info" title="Unassigned accounts" count={data.unassigned_accounts.length}>
              {data.unassigned_accounts.length > 0 && (
                <Virtuoso
                  useWindowScroll
                  data={data.unassigned_accounts}
                  itemContent={(_, a) => (
                    <AccountQueueRow
                      key={a.account_id}
                      onClick={() => openAccount(a.account_id)}
                      username={a.marzban_username}
                      owner={null}
                      metric={<span className="text-xs text-muted-foreground">assign a customer</span>}
                    />
                  )}
                />
              )}
            </QueueSection>

            <QueueSection icon={Tag} tone="info" title="No rate configured" count={data.no_rate_accounts.length}>
              {data.no_rate_accounts.length > 0 && (
                <Virtuoso
                  useWindowScroll
                  data={data.no_rate_accounts}
                  itemContent={(_, a) => (
                    <AccountQueueRow
                      key={a.account_id}
                      onClick={() => openAccount(a.account_id)}
                      username={a.marzban_username}
                      owner={a.owner_name}
                      metric={<span className="text-xs text-muted-foreground">would bill 0 T</span>}
                    />
                  )}
                />
              )}
            </QueueSection>

            <QueueSection
              icon={RefreshCw}
              tone="warn"
              title="Pending settlement"
              count={data.pending_settlement.length}
            >
              {data.pending_settlement.length > 0 && (
                <Virtuoso
                  useWindowScroll
                  data={data.pending_settlement}
                  itemContent={(_, p) => (
                    p.type === 'group' ? (
                      <Link
                        key={`group-${p.id}`}
                        to={`/groups/${p.id}`}
                        className="flex items-center justify-between gap-3 px-4 py-1.5 text-[13px] hover:bg-muted/50"
                      >
                        <span className="flex min-w-0 items-baseline gap-2">
                          <span className="truncate">{p.name}</span>
                          <span className="shrink-0 text-[11px] text-muted-foreground">
                            {p.billing_mode === 'payg' ? 'pay-as-you-go' : 'prepay'}
                          </span>
                        </span>
                        <span className="shrink-0 text-xs text-muted-foreground">
                          {p.is_due && (
                            <>
                              <span className="font-medium text-destructive">{p.days_overdue}d overdue</span>
                              {' · '}
                            </>
                          )}
                          <QueueAmount netOwed={p.net_owed} pending={p.pending_amount} />
                        </span>
                      </Link>
                    ) : (
                      <button
                        key={`account-${p.id}`}
                        type="button"
                        onClick={() => openAccount(p.id)}
                        className="flex items-center justify-between gap-3 px-4 py-1.5 text-left text-[13px] hover:bg-muted/50"
                      >
                        <span className="truncate font-mono text-xs font-medium">{p.name}</span>
                        <span className="shrink-0 text-xs text-muted-foreground">
                          <QueueAmount netOwed={p.net_owed} pending={p.pending_amount} />
                        </span>
                      </button>
                    )
                  )}
                />
              )}
            </QueueSection>

            {/* Last, deliberately: debts are real but not "service about to
                break" urgent — see the note at the top of this list. */}
            <QueueSection icon={Wallet} tone="danger" title="Customers in debt" count={data.overdue_customers.length}>
              {data.overdue_customers.length > 0 && (
                <Virtuoso
                  useWindowScroll
                  data={data.overdue_customers}
                  itemContent={(_, c) => (
                    <Link
                      key={c.customer_id}
                      to={`/customers/${c.customer_id}`}
                      className="flex items-center justify-between gap-3 px-4 py-1.5 text-[13px] hover:bg-muted/50"
                    >
                      <span className="truncate">{c.name}</span>
                      <Money amount={c.balance} className="text-xs" />
                    </Link>
                  )}
                />
              )}
            </QueueSection>
          </div>
        )}
      </div>
    </div>
  )
}

/** What this entity owes right now (posted debt + not-yet-invoiced usage,
 * netted — see AccountRow.net_owed), with the uninvoiced part named
 * separately only as a secondary note. Showing the two as competing figures
 * made an entity that had just been paid off still read as owing. */
function QueueAmount({ netOwed, pending }: { netOwed: number; pending: number }) {
  return (
    <>
      {netOwed > 0 ? (
        <span className="font-medium text-destructive">{formatToman(netOwed)} owed</span>
      ) : netOwed < 0 ? (
        <span className="font-medium text-credit">{formatToman(Math.abs(netOwed))} cr</span>
      ) : (
        <span>settled</span>
      )}
      {pending > 0 && <span className="text-muted-foreground"> · {formatToman(pending)} to invoice</span>}
    </>
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
  hasNextPlan,
}: {
  username: string
  owner: string | null
  metric: React.ReactNode
  onClick: () => void
  // Only passed for the expired/exhausted/near-expiry/near-quota buckets —
  // the ones where "has this already been covered" is exactly the question
  // being asked. Omitted (undefined) elsewhere so it stays silent where it
  // doesn't apply (unassigned accounts, no rate configured).
  hasNextPlan?: boolean
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
      <span className="flex shrink-0 items-center gap-2">
        {hasNextPlan !== undefined && (
          hasNextPlan ? (
            <span className="inline-flex items-center rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
              Next ▸
            </span>
          ) : (
            <span className="inline-flex items-center rounded bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium text-warning">
              No next plan
            </span>
          )
        )}
        <span className="text-xs tabular-nums">{metric}</span>
      </span>
    </button>
  )
}
