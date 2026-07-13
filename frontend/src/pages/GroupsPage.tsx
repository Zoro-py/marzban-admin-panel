import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { groupsApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { NewGroupDialog } from '@/components/groups/NewGroupDialog'
import { formatBytes, formatToman } from '@/lib/utils'

export function GroupsPage() {
  const { data, isLoading } = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Groups</h1>
          <p className="text-sm text-muted-foreground">Pay-as-you-go billing groups — e.g. a company settling usage monthly.</p>
        </div>
        <NewGroupDialog />
      </div>

      <div className="rounded-xl border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Accounts</TableHead>
              <TableHead>Usage (lifetime)</TableHead>
              <TableHead>Rate</TableHead>
              <TableHead className="text-right">Balance</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {!isLoading && data?.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground">
                  No groups yet.
                </TableCell>
              </TableRow>
            )}
            {data?.map((g) => (
              <TableRow key={g.id}>
                <TableCell>
                  <Link to={`/groups/${g.id}`} className="font-medium hover:underline">
                    {g.name}
                  </Link>
                </TableCell>
                <TableCell>{g.account_count}</TableCell>
                <TableCell>{formatBytes(g.total_used_traffic)}</TableCell>
                <TableCell className="text-muted-foreground">{g.rate_per_gb ? `${formatToman(g.rate_per_gb)}/GB` : '—'}</TableCell>
                <TableCell className="text-right">
                  {g.balance === 0 ? (
                    <Badge variant="success">settled</Badge>
                  ) : g.balance > 0 ? (
                    <Badge variant="destructive">{formatToman(g.balance)} owed</Badge>
                  ) : (
                    <Badge variant="secondary">{formatToman(Math.abs(g.balance))} credit</Badge>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
