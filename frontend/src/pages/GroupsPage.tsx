import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Clock } from 'lucide-react'
import { groupsApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { NewGroupDialog } from '@/components/groups/NewGroupDialog'
import { formatBytes, formatToman } from '@/lib/utils'

export function GroupsPage() {
  const { data, isLoading } = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Groups</h1>
          <p className="text-sm text-muted-foreground">
            Billing units that bundle several accounts under one customer's invoice — e.g. a company settling usage
            for all its employee accounts on a cycle.
          </p>
        </div>
        <NewGroupDialog />
      </div>

      <div className="rounded-xl border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Mode</TableHead>
              <TableHead>Accounts</TableHead>
              <TableHead>Usage (this cycle)</TableHead>
              <TableHead>Rate</TableHead>
              <TableHead className="text-right">Balance</TableHead>
              <TableHead className="text-right">Pending</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {!isLoading && data?.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} className="text-center text-muted-foreground">
                  No groups yet.
                </TableCell>
              </TableRow>
            )}
            {data?.map((g) => (
              <TableRow key={g.id}>
                <TableCell>
                  <div className="flex items-center gap-1.5">
                    <Link to={`/groups/${g.id}`} className="font-medium hover:underline">
                      {g.name}
                    </Link>
                    {g.is_due && g.billing_mode === 'payg' && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <span>
                            <Badge variant="warning" className="gap-1">
                              <Clock className="h-3 w-3" /> due
                            </Badge>
                          </span>
                        </TooltipTrigger>
                        <TooltipContent>
                          Its {g.billing_cycle_days}-day cycle has elapsed since{' '}
                          {g.last_settled_at ? 'the last settle' : 'it was created'} — settle it to charge and start
                          the next cycle.
                        </TooltipContent>
                      </Tooltip>
                    )}
                  </div>
                </TableCell>
                <TableCell>
                  <Badge variant={g.billing_mode === 'payg' ? 'outline' : 'secondary'} className="capitalize">
                    {g.billing_mode}
                  </Badge>
                </TableCell>
                <TableCell>{g.account_count}</TableCell>
                <TableCell>
                  <div className="flex flex-col">
                    <span className="font-mono tabular-nums">{formatBytes(g.current_cycle_used_bytes)}</span>
                    <span className="text-xs text-muted-foreground">{formatBytes(g.total_used_traffic)} lifetime</span>
                  </div>
                </TableCell>
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
                <TableCell className="text-right">
                  {g.pending_amount > 0 ? (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="font-mono tabular-nums text-warning">{formatToman(g.pending_amount)}</span>
                      </TooltipTrigger>
                      <TooltipContent>
                        Not charged yet — this is what settling right now would add to the balance above.
                      </TooltipContent>
                    </Tooltip>
                  ) : (
                    <span className="text-muted-foreground">—</span>
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
