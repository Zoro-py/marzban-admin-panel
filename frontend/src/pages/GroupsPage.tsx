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
      <div className="flex flex-wrap items-center justify-between gap-2">
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
              {/* Name and what they owe hold at every width; the rest drop
                  out progressively so a phone gets a readable two-column
                  table instead of a seven-column side-scroll. */}
              <TableHead>Name</TableHead>
              <TableHead className="hidden sm:table-cell">Mode</TableHead>
              <TableHead className="hidden text-right lg:table-cell">Members</TableHead>
              <TableHead className="hidden md:table-cell">Usage this cycle</TableHead>
              <TableHead className="hidden text-right xl:table-cell">Rate</TableHead>
              <TableHead className="hidden text-right lg:table-cell">Not invoiced</TableHead>
              <TableHead className="text-right">Owes now</TableHead>
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
                <TableCell className="hidden sm:table-cell">
                  <Badge variant={g.billing_mode === 'payg' ? 'warning' : 'secondary'}>
                    {g.billing_mode === 'payg' ? 'pay-as-you-go' : 'prepay'}
                  </Badge>
                </TableCell>
                <TableCell className="hidden text-right tabular-nums lg:table-cell">{g.account_count}</TableCell>
                <TableCell className="hidden md:table-cell">
                  <span className="flex flex-col leading-tight">
                    <span className="font-mono text-xs tabular-nums">{formatBytes(g.current_cycle_used_bytes)}</span>
                    <span className="text-[11px] text-muted-foreground">{formatBytes(g.total_used_traffic)} lifetime</span>
                  </span>
                </TableCell>
                <TableCell className="hidden text-right text-xs tabular-nums text-muted-foreground xl:table-cell">
                  {g.rate_per_gb ? `${formatToman(g.rate_per_gb)}/GB` : '—'}
                </TableCell>
                <TableCell className="hidden text-right lg:table-cell">
                  <Money amount={g.pending_amount} kind="pending" className="text-xs" />
                </TableCell>
                <TableCell className="text-right">
                  <Money amount={g.net_owed} zero="settled" className="text-xs" />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
