import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { accountsApi, groupsApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { SortableHeader, nextSort, type SortState } from '@/components/ui/sortable-header'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { AccountsBoard } from '@/components/accounts/AccountsBoard'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { UsageBar } from '@/components/UsageBar'
import { Money } from '@/components/Money'
import { StatusDot } from '@/components/StatusDot'
import type { AccountRow } from '@/lib/types'
import { cn, daysUntil, formatDate, formatToman } from '@/lib/utils'
import { Search } from 'lucide-react'

type View = 'all' | 'attention' | 'unassigned' | 'debt' | 'payg' | 'no_rate' | 'disabled'

const VIEWS: { id: View; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'attention', label: 'Needs attention' },
  { id: 'debt', label: 'In debt' },
  { id: 'unassigned', label: 'Unassigned' },
  { id: 'payg', label: 'Pay-as-you-go' },
  { id: 'no_rate', label: 'No rate' },
  { id: 'disabled', label: 'Disabled' },
]

function needsAttention(a: AccountRow): boolean {
  const pct = a.data_limit ? (a.used_traffic / a.data_limit) * 100 : null
  const days = daysUntil(a.expire)
  return (pct !== null && pct >= 80) || (days !== null && days <= 3)
}

function matchesView(a: AccountRow, view: View): boolean {
  switch (view) {
    case 'all':
      return true
    case 'attention':
      return needsAttention(a)
    case 'unassigned':
      return a.customer_id === null && a.group_id === null
    case 'debt':
      return a.payer_balance > 0
    case 'payg':
      return a.billing_mode === 'payg'
    case 'no_rate':
      return !a.rate_configured
    case 'disabled':
      return a.status !== 'active'
  }
}

export function AccountsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const openAccount = useOpenAccountInspector()
  const selectedId = searchParams.get('acct')
  const [search, setSearch] = React.useState('')
  const [view, setView] = React.useState<View>('all')
  const [sort, setSort] = React.useState<SortState | null>(null)

  // Back-compat: old deep links used ?highlight=<id> — same intent, new surface.
  React.useEffect(() => {
    const legacy = searchParams.get('highlight')
    if (legacy) {
      const next = new URLSearchParams(searchParams)
      next.delete('highlight')
      next.set('acct', legacy)
      setSearchParams(next, { replace: true })
    }
  }, [searchParams, setSearchParams])

  const accountsQuery = useQuery({ queryKey: ['accounts'], queryFn: () => accountsApi.list() })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })
  const groupById = React.useMemo(() => new Map(groupsQuery.data?.map((g) => [g.id, g])), [groupsQuery.data])

  const filtered = React.useMemo(() => {
    if (!accountsQuery.data) return []
    let rows = accountsQuery.data

    const q = search.trim().toLowerCase()
    if (q) {
      rows = rows.filter(
        (a) =>
          a.marzban_username.toLowerCase().includes(q) ||
          a.customer_name?.toLowerCase().includes(q) ||
          a.group_name?.toLowerCase().includes(q),
      )
    }
    rows = rows.filter((a) => matchesView(a, view))

    if (sort) {
      const dir = sort.dir === 'asc' ? 1 : -1
      rows = [...rows].sort((a, b) => dir * compareBy(sort.key, a, b))
    }
    return rows
  }, [accountsQuery.data, search, view, sort])

  const viewCounts = React.useMemo(() => {
    const counts = new Map<View, number>()
    for (const v of VIEWS) counts.set(v.id, accountsQuery.data?.filter((a) => matchesView(a, v.id)).length ?? 0)
    return counts
  }, [accountsQuery.data])

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Accounts</h1>
          <p className="text-xs text-muted-foreground">
            Every Marzban user, with synced usage. Click a row for details &amp; actions.
          </p>
        </div>
        <NewAccountDialog />
      </div>

      <Tabs defaultValue="table">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1">
            {VIEWS.map((v) => {
              const count = viewCounts.get(v.id) ?? 0
              if (v.id !== 'all' && count === 0) return null
              return (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => setView(v.id)}
                  className={cn(
                    'flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium transition-colors',
                    view === v.id ? 'bg-accent text-foreground' : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
                  )}
                >
                  {v.label}
                  <span className={cn('tabular-nums', view === v.id ? 'text-muted-foreground' : 'text-muted-foreground/60')}>
                    {count}
                  </span>
                </button>
              )
            })}
          </div>
          <TabsList>
            <TabsTrigger value="table">Table</TabsTrigger>
            <TabsTrigger value="board">Board</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="table" className="mt-3 flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <div className="relative max-w-xs flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Filter by username, customer, or group…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8"
              />
            </div>
            <span className="ml-auto text-xs tabular-nums text-muted-foreground">
              {filtered.length} of {accountsQuery.data?.length ?? 0}
            </span>
          </div>

          <div className="overflow-hidden rounded-lg border border-border bg-card">
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableHeader label="Account" sortKey="username" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} />
                  <SortableHeader label="Billed to" sortKey="owner" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} />
                  <SortableHeader label="Usage" sortKey="usage_pct" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} />
                  <TableHead>Avg/mo</TableHead>
                  <SortableHeader label="Expires" sortKey="expires" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} />
                  <SortableHeader label="Rate" sortKey="rate" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} className="text-right" align="right" />
                  <SortableHeader label="Balance" sortKey="balance" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} className="text-right" align="right" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {accountsQuery.isLoading && (
                  <TableRow>
                    <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                      Loading…
                    </TableCell>
                  </TableRow>
                )}
                {!accountsQuery.isLoading && filtered.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                      No accounts match this view.
                    </TableCell>
                  </TableRow>
                )}
                {filtered.map((a) => (
                  <AccountTableRow
                    key={a.id}
                    account={a}
                    groupName={a.group_id ? groupById.get(a.group_id)?.name ?? a.group_name : null}
                    selected={String(a.id) === selectedId}
                    onOpen={() => openAccount(a.id)}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        </TabsContent>

        <TabsContent value="board" className="mt-3">
          <AccountsBoard accounts={accountsQuery.data ?? []} groups={groupsQuery.data ?? []} isLoading={accountsQuery.isLoading} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function AccountTableRow({
  account: a,
  groupName,
  selected,
  onOpen,
}: {
  account: AccountRow
  groupName: string | null
  selected: boolean
  onOpen: () => void
}) {
  const days = daysUntil(a.expire)
  return (
    <TableRow
      data-state={selected ? 'selected' : undefined}
      onClick={onOpen}
      className="cursor-pointer"
    >
      <TableCell>
        <span className="flex items-center gap-2">
          <StatusDot status={a.status} />
          <span className="font-mono text-xs font-medium">{a.marzban_username}</span>
        </span>
      </TableCell>
      <TableCell>
        {a.customer_id || a.group_id ? (
          <span className="flex flex-col leading-tight">
            {a.customer_id && (
              <Link
                to={`/customers/${a.customer_id}`}
                onClick={(e) => e.stopPropagation()}
                className="w-fit text-[13px] hover:underline"
              >
                {a.customer_name ?? `#${a.customer_id}`}
              </Link>
            )}
            {a.group_id && (
              <Link
                to={`/groups/${a.group_id}`}
                onClick={(e) => e.stopPropagation()}
                className="w-fit text-[11px] text-muted-foreground hover:underline"
              >
                {groupName ?? `group #${a.group_id}`}
              </Link>
            )}
          </span>
        ) : (
          <Badge variant="warning">unassigned</Badge>
        )}
      </TableCell>
      <TableCell>
        <UsageBar used={a.used_traffic} limit={a.data_limit} compact />
      </TableCell>
      <TableCell>
        {a.usage_confidence === 'insufficient_data' ? (
          <span className="text-[11px] text-muted-foreground/60">—</span>
        ) : (
          <span
            className="text-xs tabular-nums text-muted-foreground"
            title={
              a.usage_confidence === 'preliminary'
                ? `Extrapolated from only ${a.usage_sample_days} days of history — settles after 30 days.`
                : `Averaged over ${a.usage_sample_days} days of history.`
            }
          >
            {a.usage_confidence === 'preliminary' ? '~' : ''}
            {a.monthly_avg_usage_gb?.toFixed(1)} GB
          </span>
        )}
      </TableCell>
      <TableCell>
        {a.expire === null ? (
          <span className="text-xs text-muted-foreground">never</span>
        ) : (
          <span
            className={cn(
              'text-xs tabular-nums',
              days !== null && days < 0 && 'font-medium text-destructive',
              days !== null && days >= 0 && days <= 3 && 'font-medium text-warning',
            )}
            title={formatDate(a.expire)}
          >
            {days! < 0 ? `${Math.abs(days!)}d ago` : `${days}d`}
          </span>
        )}
      </TableCell>
      <TableCell className="text-right">
        {!a.rate_configured ? (
          <Badge variant="warning">not set</Badge>
        ) : a.effective_rate > 0 ? (
          <span className="text-xs tabular-nums text-muted-foreground">{formatToman(a.effective_rate)}/GB</span>
        ) : (
          <span className="text-xs text-muted-foreground">free</span>
        )}
      </TableCell>
      <TableCell className="text-right">
        <Money amount={a.payer_balance} className="text-xs" />
      </TableCell>
    </TableRow>
  )
}

function compareBy(key: string, a: AccountRow, b: AccountRow): number {
  switch (key) {
    case 'username':
      return a.marzban_username.localeCompare(b.marzban_username)
    case 'owner':
      return (a.customer_name ?? a.group_name ?? '').localeCompare(b.customer_name ?? b.group_name ?? '')
    case 'usage_pct': {
      const pctA = a.data_limit ? a.used_traffic / a.data_limit : -1
      const pctB = b.data_limit ? b.used_traffic / b.data_limit : -1
      return pctA - pctB
    }
    case 'balance':
      return a.payer_balance - b.payer_balance
    case 'rate':
      return a.effective_rate - b.effective_rate
    case 'expires':
      return (a.expire ?? Infinity) - (b.expire ?? Infinity)
    default:
      return 0
  }
}
