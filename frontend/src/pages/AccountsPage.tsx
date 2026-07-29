import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { accountsApi, groupsApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { SortableHeader, nextSort, type SortState } from '@/components/ui/sortable-header'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { SettleAccountButton } from '@/components/accounts/SettleAccountButton'
import { AccountsBoard } from '@/components/accounts/AccountsBoard'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { EmptyState } from '@/components/EmptyState'
import { UsageBar } from '@/components/UsageBar'
import { Money } from '@/components/Money'
import { StatusDot } from '@/components/StatusDot'
import type { AccountRow } from '@/lib/types'
import { cn, daysUntil, formatDate, formatToman } from '@/lib/utils'
import { Skeleton } from '@/components/ui/skeleton'
import { Search } from 'lucide-react'

type View = 'all' | 'attention' | 'unassigned' | 'debt' | 'payg' | 'no_rate' | 'disabled' | 'deleted'

const VIEWS: { id: View; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'attention', label: 'Needs attention' },
  { id: 'debt', label: 'In debt' },
  { id: 'unassigned', label: 'Unassigned' },
  { id: 'payg', label: 'Pay-as-you-go' },
  { id: 'no_rate', label: 'No rate' },
  { id: 'disabled', label: 'Disabled' },
  { id: 'deleted', label: 'Deleted from Marzban' },
]

function needsAttention(a: AccountRow): boolean {
  const pct = a.data_limit ? (a.used_traffic / a.data_limit) * 100 : null
  const days = daysUntil(a.expire)
  return (pct !== null && pct >= 80) || (days !== null && days <= 3)
}

function matchesView(a: AccountRow, view: View): boolean {
  switch (view) {
    case 'all':
      // Deleted accounts have their own dedicated tab — don't clutter the main list.
      return a.status !== 'deleted_from_marzban'
    case 'attention':
      return a.status !== 'deleted_from_marzban' && needsAttention(a)
    case 'unassigned':
      return a.status !== 'deleted_from_marzban' && a.customer_id === null && a.group_id === null
    case 'debt':
      return a.status !== 'deleted_from_marzban' && a.net_owed > 0
    case 'payg':
      return a.status !== 'deleted_from_marzban' && a.effective_billing_mode === 'payg'
    case 'no_rate':
      return a.status !== 'deleted_from_marzban' && !a.rate_configured
    case 'disabled':
      return a.status !== 'deleted_from_marzban' && a.status !== 'active'
    case 'deleted':
      return a.status === 'deleted_from_marzban'
  }
}

export function AccountsPage() {
  React.useEffect(() => {
    document.title = 'Shiraze | Accounts'
  }, [])

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
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
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
            <div className="relative flex-1 sm:max-w-xs">
              <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder="Filter accounts…"
                value={search || ''}
                onChange={(e) => setSearch(e.target.value)}
                className="pl-8"
              />
            </div>
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
              {filtered.length} of {accountsQuery.data?.length ?? 0}
            </span>
          </div>

          <div className="overflow-hidden rounded-lg border border-border bg-card">
            <Table>
              <TableHeader>
                <TableRow>
                  {/* Account, usage and what they owe survive at every width —
                      the rest drop out progressively rather than forcing a
                      seven-column table to be side-scrolled on a phone. The
                      hidden owner reappears under the username below. */}
                  <SortableHeader label="Account" sortKey="username" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} />
                  <SortableHeader label="Billed to" sortKey="owner" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} className="hidden md:table-cell" />
                  <SortableHeader label="Usage" sortKey="usage_pct" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} />
                  <TableHead className="hidden xl:table-cell">Avg/mo</TableHead>
                  <SortableHeader label="Expires" sortKey="expires" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} className="hidden sm:table-cell" />
                  <SortableHeader label="Rate" sortKey="rate" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} className="hidden text-right lg:table-cell" align="right" />
                  <SortableHeader label="Owes now" sortKey="balance" sort={sort} onSort={(k) => setSort((c) => nextSort(c, k))} className="text-right" align="right" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {accountsQuery.isLoading && (
                  Array.from({ length: 5 }).map((_, i) => (
                    <TableRow key={i}>
                      <TableCell><Skeleton className="h-4 w-24" /></TableCell>
                      <TableCell className="hidden md:table-cell"><Skeleton className="h-4 w-20" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-full" /></TableCell>
                      <TableCell className="hidden xl:table-cell"><Skeleton className="h-4 w-12" /></TableCell>
                      <TableCell className="hidden sm:table-cell"><Skeleton className="h-4 w-16" /></TableCell>
                      <TableCell className="hidden text-right lg:table-cell"><Skeleton className="h-4 w-16 ml-auto" /></TableCell>
                      <TableCell className="text-right"><Skeleton className="h-4 w-20 ml-auto" /></TableCell>
                    </TableRow>
                  ))
                )}
                {!accountsQuery.isLoading && filtered.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="py-8">
                      <EmptyState title="No accounts match this view." />
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
          <span className="flex min-w-0 flex-col leading-tight">
            <span className="flex items-center gap-1.5">
              <span className="truncate font-mono text-xs font-medium">{a.marzban_username}</span>
              {a.has_next_plan && (
                <span className="inline-flex shrink-0 items-center rounded bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                  Next ▸
                </span>
              )}
            </span>
            {/* Owner moves in here once its own column is hidden, so a phone
                still answers "whose account is this?". */}
            <span className="truncate text-[11px] text-muted-foreground md:hidden">
              {a.customer_name ?? groupName ?? a.group_name ?? 'unassigned'}
            </span>
          </span>
        </span>
      </TableCell>
      <TableCell className="hidden md:table-cell">
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
      <TableCell className="hidden xl:table-cell">
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
      <TableCell className="hidden sm:table-cell">
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
      <TableCell className="hidden text-right lg:table-cell">
        {!a.rate_configured ? (
          <Badge variant="warning">not set</Badge>
        ) : a.effective_rate > 0 ? (
          <span className="text-xs tabular-nums text-muted-foreground">{formatToman(a.effective_rate)}/GB</span>
        ) : (
          <span className="text-xs text-muted-foreground">free</span>
        )}
      </TableCell>
      <TableCell className="text-right">
        {/* ONE number: what they owe right now (posted debt + not-yet-invoiced
            usage, netted). "settled" rather than a bare dash for zero — a dash
            reads as "no data". The uninvoiced part is NOT printed alongside it:
            on an account that has already paid, "243,916 not invoiced yet"
            under "13,916 owed" reads as a contradiction, because in isolation
            it omits the payment that cancels most of it. The full arithmetic
            lives in the tooltip, where it always reconciles. */}
        <span className="flex flex-col items-end gap-0.5">
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="cursor-help">
                <Money amount={a.net_owed} zero="settled" className="text-xs" />
              </span>
            </TooltipTrigger>
            <TooltipContent>
              <span className="flex flex-col gap-0.5 text-xs tabular-nums">
                <span>{formatToman(a.pending_amount)} usage not invoiced yet</span>
                <span>
                  {a.payer_balance >= 0 ? '+ ' : '− '}
                  {formatToman(Math.abs(a.payer_balance))} {a.payer_balance >= 0 ? 'invoiced, unpaid' : 'already paid'}
                </span>
                <span className="border-t border-border/50 pt-0.5 font-medium">
                  = {formatToman(a.net_owed)} owed
                </span>
              </span>
            </TooltipContent>
          </Tooltip>
          {/* Only standalone accounts settle individually — a grouped
              account's pending is settled via its group's own button. */}
          {a.pending_amount > 0 && a.group_id === null && (
            <SettleAccountButton
              accountId={a.id}
              username={a.marzban_username}
              amount={a.pending_amount}
              currentBalance={a.payer_balance}
            />
          )}
        </span>
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
      return a.net_owed - b.net_owed
    case 'rate':
      return a.effective_rate - b.effective_rate
    case 'expires':
      return (a.expire ?? Infinity) - (b.expire ?? Infinity)
    default:
      return 0
  }
}
