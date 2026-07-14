import * as React from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { accountsApi, groupsApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { SortableHeader, nextSort, type SortState } from '@/components/ui/sortable-header'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { AdjustAccountDialog } from '@/components/accounts/AdjustAccountDialog'
import { RelationshipDialog } from '@/components/accounts/RelationshipDialog'
import { ResetUsageDialog } from '@/components/accounts/ResetUsageDialog'
import { BillingDialog } from '@/components/accounts/BillingDialog'
import { InvoiceDialog } from '@/components/accounts/InvoiceDialog'
import { UsageBar } from '@/components/UsageBar'
import { AccountsBoard } from '@/components/accounts/AccountsBoard'
import type { AccountRow } from '@/lib/types'
import { formatDate, formatToman } from '@/lib/utils'
import { Search, SlidersHorizontal, Info } from 'lucide-react'

interface Filters {
  status: 'all' | 'active' | 'disabled'
  billingMode: 'all' | 'prepay' | 'payg'
  debtOnly: boolean
  noRateOnly: boolean
}

const DEFAULT_FILTERS: Filters = { status: 'all', billingMode: 'all', debtOnly: false, noRateOnly: false }

function activeFilterCount(f: Filters): number {
  let n = 0
  if (f.status !== 'all') n++
  if (f.billingMode !== 'all') n++
  if (f.debtOnly) n++
  if (f.noRateOnly) n++
  return n
}

export function AccountsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const highlightId = searchParams.get('highlight')
  const location = useLocation()
  const [showUnassignedOnly, setShowUnassignedOnly] = React.useState(false)
  const [search, setSearch] = React.useState('')
  const [filters, setFilters] = React.useState<Filters>(DEFAULT_FILTERS)
  const [sort, setSort] = React.useState<SortState | null>(null)
  const highlightRef = React.useRef<HTMLTableRowElement | null>(null)
  // Tracks which (navigation, target) pair has already flashed, so a query
  // refetch triggered by an unrelated mutation elsewhere on the page (every
  // dialog invalidates ['accounts'] on success) doesn't scroll the viewport
  // back to a highlight the operator already saw and moved on from. Keyed by
  // location.key (unique per navigation, even to the identical URL) rather
  // than highlightId alone, so clicking the same account link again later
  // still flashes — only same-navigation refetches are suppressed.
  const flashedForRef = React.useRef<string | null>(null)

  const accountsQuery = useQuery({
    queryKey: ['accounts', { unassignedOnly: showUnassignedOnly }],
    queryFn: () => accountsApi.list(showUnassignedOnly ? { unassigned_only: true } : undefined),
  })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })

  const groupById = React.useMemo(() => new Map(groupsQuery.data?.map((g) => [g.id, g])), [groupsQuery.data])

  // Scroll to and briefly flash the deep-linked row (item 2 of the UI ask).
  React.useEffect(() => {
    if (!highlightId || !accountsQuery.data) return
    const flashKey = `${location.key}:${highlightId}`
    if (flashedForRef.current === flashKey) return
    const row = highlightRef.current
    if (!row) return // row not rendered yet this pass (e.g. still loading) — retry once data/sort settle
    flashedForRef.current = flashKey
    row.scrollIntoView({ behavior: 'smooth', block: 'center' })
    row.classList.remove('animate-highlight-flash')
    // force reflow so re-adding the class restarts the animation
    void row.offsetWidth
    row.classList.add('animate-highlight-flash')
  }, [highlightId, location.key, accountsQuery.data])

  const filtered = React.useMemo(() => {
    if (!accountsQuery.data) return []
    let rows = accountsQuery.data

    const q = search.trim().toLowerCase()
    if (q) rows = rows.filter((a) => a.marzban_username.toLowerCase().includes(q))

    if (filters.status !== 'all') {
      rows = rows.filter((a) => (filters.status === 'active' ? a.status === 'active' : a.status !== 'active'))
    }
    if (filters.billingMode !== 'all') {
      rows = rows.filter((a) => a.billing_mode === filters.billingMode)
    }
    if (filters.debtOnly) {
      rows = rows.filter((a) => a.payer_balance > 0)
    }
    if (filters.noRateOnly) {
      rows = rows.filter((a) => !a.rate_configured)
    }

    if (sort) {
      const dir = sort.dir === 'asc' ? 1 : -1
      rows = [...rows].sort((a, b) => dir * compareBy(sort.key, a, b))
    }

    return rows
  }, [accountsQuery.data, search, filters, sort])

  function handleSort(key: string) {
    setSort((current) => nextSort(current, key))
  }

  const activeCount = activeFilterCount(filters)

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
          <p className="text-sm text-muted-foreground">Every Marzban user mirrored here, synced usage included.</p>
        </div>
        <NewAccountDialog />
      </div>

      <Tabs defaultValue="table">
        <TabsList>
          <TabsTrigger value="table">Table</TabsTrigger>
          <TabsTrigger value="board">Board (drag to assign)</TabsTrigger>
        </TabsList>

        <TabsContent value="table" className="flex flex-col gap-4">
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative max-w-xs flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input placeholder="Search username…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-8" />
            </div>
            <Button
              variant={showUnassignedOnly ? 'default' : 'outline'}
              size="sm"
              onClick={() => setShowUnassignedOnly((v) => !v)}
            >
              Unassigned only
            </Button>

            <Popover>
              <PopoverTrigger asChild>
                <Button variant={activeCount ? 'default' : 'outline'} size="sm" className="gap-1.5">
                  <SlidersHorizontal className="h-3.5 w-3.5" />
                  Filters
                  {activeCount > 0 && (
                    <Badge variant="secondary" className="ml-0.5 h-4 min-w-4 justify-center px-1 text-[10px]">
                      {activeCount}
                    </Badge>
                  )}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="flex flex-col gap-4">
                <div className="flex flex-col gap-1.5">
                  <Label className="text-xs text-muted-foreground">Status</Label>
                  <div className="flex gap-1.5">
                    {(['all', 'active', 'disabled'] as const).map((s) => (
                      <Button
                        key={s}
                        size="sm"
                        variant={filters.status === s ? 'default' : 'outline'}
                        onClick={() => setFilters((f) => ({ ...f, status: s }))}
                        className="capitalize"
                      >
                        {s}
                      </Button>
                    ))}
                  </div>
                </div>

                <div className="flex flex-col gap-1.5">
                  <Label className="text-xs text-muted-foreground">Billing mode</Label>
                  <div className="flex gap-1.5">
                    {(['all', 'prepay', 'payg'] as const).map((m) => (
                      <Button
                        key={m}
                        size="sm"
                        variant={filters.billingMode === m ? 'default' : 'outline'}
                        onClick={() => setFilters((f) => ({ ...f, billingMode: m }))}
                        className="capitalize"
                      >
                        {m}
                      </Button>
                    ))}
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <Checkbox
                    id="filter-debt"
                    checked={filters.debtOnly}
                    onCheckedChange={(c) => setFilters((f) => ({ ...f, debtOnly: c === true }))}
                  />
                  <Label htmlFor="filter-debt" className="cursor-pointer font-normal">
                    Carrying debt only
                  </Label>
                </div>
                <div className="flex items-center gap-2">
                  <Checkbox
                    id="filter-no-rate"
                    checked={filters.noRateOnly}
                    onCheckedChange={(c) => setFilters((f) => ({ ...f, noRateOnly: c === true }))}
                  />
                  <Label htmlFor="filter-no-rate" className="cursor-pointer font-normal">
                    No rate configured only
                  </Label>
                </div>

                {activeCount > 0 && (
                  <Button variant="ghost" size="sm" onClick={() => setFilters(DEFAULT_FILTERS)}>
                    Clear filters
                  </Button>
                )}
              </PopoverContent>
            </Popover>

            {highlightId && (
              <Button variant="ghost" size="sm" onClick={() => setSearchParams({})}>
                Clear highlight
              </Button>
            )}

            <span className="ml-auto text-xs text-muted-foreground">
              {filtered.length} of {accountsQuery.data?.length ?? 0}
            </span>
          </div>

          <div className="rounded-xl border border-border bg-card">
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableHeader label="Username" sortKey="username" sort={sort} onSort={handleSort} />
                  <SortableHeader label="Owner" sortKey="owner" sort={sort} onSort={handleSort} />
                  <TableHead>Group</TableHead>
                  <SortableHeader label="Usage" sortKey="usage_pct" sort={sort} onSort={handleSort} />
                  <SortableHeader label="Balance" sortKey="balance" sort={sort} onSort={handleSort} />
                  <SortableHeader label="Rate" sortKey="rate" sort={sort} onSort={handleSort} />
                  <SortableHeader label="Expires" sortKey="expires" sort={sort} onSort={handleSort} />
                  <SortableHeader label="Status" sortKey="status" sort={sort} onSort={handleSort} />
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {accountsQuery.isLoading && (
                  <TableRow>
                    <TableCell colSpan={9} className="text-center text-muted-foreground">
                      Loading…
                    </TableCell>
                  </TableRow>
                )}
                {!accountsQuery.isLoading && filtered.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={9} className="text-center text-muted-foreground">
                      No accounts match these filters.
                    </TableCell>
                  </TableRow>
                )}
                {filtered.map((a) => {
                  const group = a.group_id ? groupById.get(a.group_id) : undefined
                  const isHighlighted = String(a.id) === highlightId
                  return (
                    <TableRow key={a.id} ref={isHighlighted ? highlightRef : undefined}>
                      <TableCell>
                        <div className="flex flex-col">
                          <span className="font-mono">{a.marzban_username}</span>
                          <MonthlyAverageUsage account={a} />
                        </div>
                      </TableCell>
                      <TableCell>
                        {a.customer_id ? (
                          <Link to={`/customers/${a.customer_id}`} className="hover:underline">
                            {a.customer_name ?? `#${a.customer_id}`}
                          </Link>
                        ) : (
                          <Badge variant="outline">unassigned</Badge>
                        )}
                      </TableCell>
                      <TableCell>
                        {group ? (
                          <Link to={`/groups/${group.id}`} className="hover:underline">
                            {group.name}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell>
                        <UsageBar used={a.used_traffic} limit={a.data_limit} />
                      </TableCell>
                      <TableCell>
                        <BalanceBadge balance={a.payer_balance} />
                      </TableCell>
                      <TableCell className="text-sm">
                        {!a.rate_configured ? (
                          <Badge variant="outline">not set</Badge>
                        ) : a.effective_rate > 0 ? (
                          <span className="tabular-nums">{formatToman(a.effective_rate)}/GB</span>
                        ) : (
                          <Badge variant="secondary">free</Badge>
                        )}
                      </TableCell>
                      <TableCell>{formatDate(a.expire)}</TableCell>
                      <TableCell>
                        <Badge variant={a.status === 'active' ? 'success' : 'outline'}>{a.status ?? 'unknown'}</Badge>
                      </TableCell>
                      <TableCell className="flex flex-wrap justify-end gap-2">
                        <AdjustAccountDialog account={a} groupRatePerGb={group?.rate_per_gb} />
                        <ResetUsageDialog account={a} />
                        <InvoiceDialog account={a} />
                        <BillingDialog account={a} groupRatePerGb={group?.rate_per_gb} />
                        <RelationshipDialog account={a} />
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        </TabsContent>

        <TabsContent value="board">
          <AccountsBoard accounts={accountsQuery.data ?? []} groups={groupsQuery.data ?? []} isLoading={accountsQuery.isLoading} />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function BalanceBadge({ balance }: { balance: number }) {
  if (balance > 0) return <Badge variant="destructive">{formatToman(balance)}</Badge>
  if (balance < 0) return <Badge variant="success">{formatToman(balance)}</Badge>
  return <span className="text-sm text-muted-foreground">—</span>
}

/** Item 14 of the UI ask, built to never show a number an operator would
 * reasonably mistake for a settled figure: no history yet -> no number at
 * all (a "~" would be misleadingly precise), some history -> a number
 * prefixed "~" with a tooltip explaining it's still extrapolating, a full
 * month or more -> a plain number. */
function MonthlyAverageUsage({ account }: { account: AccountRow }) {
  if (account.usage_confidence === 'insufficient_data') {
    return <span className="text-xs text-muted-foreground">not enough history yet</span>
  }

  const label = `${account.usage_confidence === 'preliminary' ? '~' : ''}${account.monthly_avg_usage_gb?.toFixed(1)} GB/mo`
  const tooltip =
    account.usage_confidence === 'preliminary'
      ? `Extrapolated from only ${account.usage_sample_days} days of history — settles after 30 days.`
      : `Averaged over ${account.usage_sample_days} days of history.`

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex w-fit items-center gap-1 text-xs text-muted-foreground">
          {label}
          {account.usage_confidence === 'preliminary' && <Info className="h-3 w-3 opacity-60" />}
        </span>
      </TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  )
}

function compareBy(key: string, a: AccountRow, b: AccountRow): number {
  switch (key) {
    case 'username':
      return a.marzban_username.localeCompare(b.marzban_username)
    case 'owner':
      return (a.customer_name ?? '').localeCompare(b.customer_name ?? '')
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
    case 'status':
      return (a.status ?? '').localeCompare(b.status ?? '')
    default:
      return 0
  }
}
