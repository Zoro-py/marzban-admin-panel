import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { accountsApi, customersApi, groupsApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { AdjustAccountDialog } from '@/components/accounts/AdjustAccountDialog'
import { RelationshipDialog } from '@/components/accounts/RelationshipDialog'
import { formatBytes, formatDate, cn } from '@/lib/utils'
import { Search } from 'lucide-react'

export function AccountsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const highlightId = searchParams.get('highlight')
  const [showUnassignedOnly, setShowUnassignedOnly] = React.useState(false)
  const [search, setSearch] = React.useState('')

  const accountsQuery = useQuery({
    queryKey: ['accounts', { unassignedOnly: showUnassignedOnly }],
    queryFn: () => accountsApi.list(showUnassignedOnly ? { unassigned_only: true } : undefined),
  })
  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })

  const customerNameById = React.useMemo(
    () => new Map(customersQuery.data?.map((c) => [c.id, c.name])),
    [customersQuery.data],
  )
  const groupNameById = React.useMemo(() => new Map(groupsQuery.data?.map((g) => [g.id, g.name])), [groupsQuery.data])

  const filtered = React.useMemo(() => {
    if (!accountsQuery.data) return []
    const q = search.trim().toLowerCase()
    if (!q) return accountsQuery.data
    return accountsQuery.data.filter((a) => a.marzban_username.toLowerCase().includes(q))
  }, [accountsQuery.data, search])

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Accounts</h1>
          <p className="text-sm text-muted-foreground">Every Marzban user mirrored here, synced usage included.</p>
        </div>
        <NewAccountDialog />
      </div>

      <div className="flex items-center gap-3">
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
        {highlightId && (
          <Button variant="ghost" size="sm" onClick={() => setSearchParams({})}>
            Clear highlight
          </Button>
        )}
      </div>

      <div className="rounded-xl border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Username</TableHead>
              <TableHead>Owner</TableHead>
              <TableHead>Group</TableHead>
              <TableHead>Used</TableHead>
              <TableHead>Limit</TableHead>
              <TableHead>Expires</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {accountsQuery.isLoading && (
              <TableRow>
                <TableCell colSpan={8} className="text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {!accountsQuery.isLoading && filtered.length === 0 && (
              <TableRow>
                <TableCell colSpan={8} className="text-center text-muted-foreground">
                  No accounts found.
                </TableCell>
              </TableRow>
            )}
            {filtered.map((a) => (
              <TableRow key={a.id} className={cn(String(a.id) === highlightId && 'bg-primary/5 ring-1 ring-inset ring-primary/30')}>
                <TableCell className="font-mono">{a.marzban_username}</TableCell>
                <TableCell>
                  {a.customer_id ? (
                    <Link to={`/customers/${a.customer_id}`} className="hover:underline">
                      {customerNameById.get(a.customer_id) ?? `#${a.customer_id}`}
                    </Link>
                  ) : (
                    <Badge variant="outline">unassigned</Badge>
                  )}
                </TableCell>
                <TableCell>
                  {a.group_id ? (
                    <Link to={`/groups/${a.group_id}`} className="hover:underline">
                      {groupNameById.get(a.group_id) ?? `#${a.group_id}`}
                    </Link>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell>{formatBytes(a.used_traffic)}</TableCell>
                <TableCell>{formatBytes(a.data_limit)}</TableCell>
                <TableCell>{formatDate(a.expire)}</TableCell>
                <TableCell>
                  <Badge variant={a.status === 'active' ? 'success' : 'outline'}>{a.status ?? 'unknown'}</Badge>
                </TableCell>
                <TableCell className="flex justify-end gap-2">
                  <AdjustAccountDialog account={a} />
                  <RelationshipDialog account={a} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
