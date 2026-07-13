import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { groupsApi, ledgerApi } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { LedgerActionDialog } from '@/components/ledger/LedgerActionDialog'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { AdjustAccountDialog } from '@/components/accounts/AdjustAccountDialog'
import { RelationshipDialog } from '@/components/accounts/RelationshipDialog'
import { SettleGroupDialog } from '@/components/groups/SettleGroupDialog'
import { formatBytes, formatDate, formatToman } from '@/lib/utils'

export function GroupDetailPage() {
  const { id } = useParams<{ id: string }>()
  const groupId = Number(id)

  const groupQuery = useQuery({ queryKey: ['groups', groupId], queryFn: () => groupsApi.get(groupId) })
  const accountsQuery = useQuery({ queryKey: ['accounts', { groupId }], queryFn: () => groupsApi.accounts(groupId) })
  const ledgerQuery = useQuery({ queryKey: ['ledger', { groupId }], queryFn: () => ledgerApi.list({ group_id: groupId }) })

  if (groupQuery.isLoading || !groupQuery.data) {
    return <p className="text-sm text-muted-foreground">Loading…</p>
  }

  const group = groupQuery.data

  return (
    <div className="flex flex-col gap-6">
      <Link to="/groups" className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> Back to groups
      </Link>

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{group.name}</h1>
          <p className="text-sm text-muted-foreground">
            {group.rate_per_gb ? `${formatToman(group.rate_per_gb)}/GB` : 'No rate set'} · every {group.billing_cycle_days} days · last
            settled {formatDate(group.last_settled_at)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {group.balance === 0 ? (
            <Badge variant="success">settled</Badge>
          ) : group.balance > 0 ? (
            <Badge variant="destructive">{formatToman(group.balance)} owed</Badge>
          ) : (
            <Badge variant="secondary">{formatToman(Math.abs(group.balance))} credit</Badge>
          )}
          <LedgerActionDialog groupId={groupId} />
          <NewAccountDialog defaultGroupId={groupId} />
          <SettleGroupDialog groupId={groupId} />
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Member accounts ({accountsQuery.data?.length ?? 0})</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Username</TableHead>
                <TableHead>Used</TableHead>
                <TableHead>Limit</TableHead>
                <TableHead>Expires</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accountsQuery.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    No accounts in this group yet.
                  </TableCell>
                </TableRow>
              )}
              {accountsQuery.data?.map((a) => (
                <TableRow key={a.id}>
                  <TableCell className="font-mono">{a.marzban_username}</TableCell>
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
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Settlement history</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Note</TableHead>
                <TableHead className="text-right">Amount</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ledgerQuery.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={4} className="text-center text-muted-foreground">
                    No transactions yet.
                  </TableCell>
                </TableRow>
              )}
              {ledgerQuery.data?.map((entry) => (
                <TableRow key={entry.id}>
                  <TableCell>{formatDate(entry.date)}</TableCell>
                  <TableCell>
                    <Badge variant={entry.type === 'charge' ? 'destructive' : 'success'}>
                      {entry.type === 'charge' ? 'debt' : 'credit'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{entry.note ?? '—'}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatToman(entry.amount)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
