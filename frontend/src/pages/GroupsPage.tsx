import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Clock } from 'lucide-react'
import { groupsApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { NewGroupDialog } from '@/components/groups/NewGroupDialog'
import { Money } from '@/components/Money'
import { formatBytes, formatToman } from '@/lib/utils'

export function GroupsPage() {
  const { data, isLoading } = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })
  const navigate = useNavigate()

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Groups</h1>
          <p className="text-xs text-muted-foreground">
            Several accounts billed as one unit — e.g. a company settling all its employees' usage on a cycle.
          </p>
        </div>
        <NewGroupDialog />
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Mode</TableHead>
              <TableHead className="text-right">Members</TableHead>
              <TableHead>Usage this cycle</TableHead>
              <TableHead className="text-right">Rate</TableHead>
              <TableHead className="text-right">Pending</TableHead>
              <TableHead className="text-right">Balance</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {!isLoading && data?.length === 0 && (
              <TableRow>
                <TableCell colSpan={7} className="py-8 text-center text-muted-foreground">
                  No groups yet.
                </TableCell>
              </TableRow>
            )}
            {data?.map((g) => (
              <TableRow key={g.id} className="cursor-pointer" onClick={() => navigate(`/groups/${g.id}`)}>
                <TableCell>
                  <span className="flex items-center gap-1.5 font-medium">
                    {g.name}
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
                          Its {g.billing_cycle_days}-day cycle has elapsed — settle it to charge this cycle's usage.
                        </TooltipContent>
                      </Tooltip>
                    )}
                  </span>
                </TableCell>
                <TableCell>
                  <Badge variant={g.billing_mode === 'payg' ? 'warning' : 'secondary'}>
                    {g.billing_mode === 'payg' ? 'pay-as-you-go' : 'prepay'}
                  </Badge>
                </TableCell>
                <TableCell className="text-right tabular-nums">{g.account_count}</TableCell>
                <TableCell>
                  <span className="flex flex-col leading-tight">
                    <span className="font-mono text-xs tabular-nums">{formatBytes(g.current_cycle_used_bytes)}</span>
                    <span className="text-[11px] text-muted-foreground">{formatBytes(g.total_used_traffic)} lifetime</span>
                  </span>
                </TableCell>
                <TableCell className="text-right text-xs tabular-nums text-muted-foreground">
                  {g.rate_per_gb ? `${formatToman(g.rate_per_gb)}/GB` : '—'}
                </TableCell>
                <TableCell className="text-right">
                  <Money amount={g.pending_amount} kind="pending" className="text-xs" />
                </TableCell>
                <TableCell className="text-right">
                  <Money amount={g.balance} zero="settled" className="text-xs" />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
