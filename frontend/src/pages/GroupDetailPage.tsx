import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ArrowLeft, Clock, Copy, Info } from 'lucide-react'
import { groupsApi, ledgerApi, apiErrorMessage } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { LedgerActionDialog } from '@/components/ledger/LedgerActionDialog'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { AdjustAccountDialog } from '@/components/accounts/AdjustAccountDialog'
import { RelationshipDialog } from '@/components/accounts/RelationshipDialog'
import { ResetUsageDialog } from '@/components/accounts/ResetUsageDialog'
import { BillingDialog } from '@/components/accounts/BillingDialog'
import { SettleGroupDialog } from '@/components/groups/SettleGroupDialog'
import { GroupSettingsDialog } from '@/components/groups/GroupSettingsDialog'
import { UsageBar } from '@/components/UsageBar'
import { formatDate, formatToman } from '@/lib/utils'

export function GroupDetailPage() {
  const { id } = useParams<{ id: string }>()
  const groupId = Number(id)
  const [copying, setCopying] = React.useState(false)

  const groupQuery = useQuery({ queryKey: ['groups', groupId], queryFn: () => groupsApi.get(groupId) })
  const accountsQuery = useQuery({ queryKey: ['accounts', { groupId }], queryFn: () => groupsApi.accounts(groupId) })
  const ledgerQuery = useQuery({ queryKey: ['ledger', { groupId }], queryFn: () => ledgerApi.list({ group_id: groupId }) })
  // Fetched eagerly (not just on "copy summary" click) so the member table can
  // show the SAME billable-GB figure the pending amount is computed from —
  // previously the table only showed Marzban's used_traffic progress bar,
  // which can legitimately differ from billable usage, and looked like the
  // pending amount didn't match anything visible on the page.
  const invoiceQuery = useQuery({ queryKey: ['groups', groupId, 'invoice'], queryFn: () => groupsApi.invoice(groupId) })
  const billableByAccount = React.useMemo(
    () => new Map(invoiceQuery.data?.lines.map((l) => [l.account_id, l])),
    [invoiceQuery.data],
  )

  if (groupQuery.isLoading || !groupQuery.data) {
    return <p className="text-sm text-muted-foreground">Loading…</p>
  }

  const group = groupQuery.data

  // Item 8 of the follow-up feedback: a one-click summary of what this cycle
  // would charge, per member, ready to paste into a chat with the customer.
  async function copySummary() {
    setCopying(true)
    try {
      const invoice = await groupsApi.invoice(groupId)
      const lines = invoice.lines
        .filter((l) => l.billable_gb > 0)
        .map((l) => `${l.marzban_username}: ${l.billable_gb} GB => ${formatToman(l.amount)}`)
      const text = [
        `${group.name} — usage since ${group.last_settled_at ? formatDate(group.last_settled_at) : 'the start'}`,
        ...lines,
        `Total: ${formatToman(invoice.total_amount)}`,
      ].join('\n')
      await navigator.clipboard.writeText(text)
      toast.success('Summary copied to clipboard')
    } catch (err) {
      toast.error(apiErrorMessage(err))
    } finally {
      setCopying(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Link to="/groups" className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> Back to groups
      </Link>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold tracking-tight">{group.name}</h1>
            <Badge variant={group.billing_mode === 'payg' ? 'outline' : 'secondary'} className="capitalize">
              {group.billing_mode}
            </Badge>
            {group.is_due && group.billing_mode === 'payg' && (
              <Badge variant="warning" className="gap-1">
                <Clock className="h-3 w-3" /> due for settlement
              </Badge>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            {group.rate_per_gb ? `${formatToman(group.rate_per_gb)}/GB` : 'No rate set'} · every {group.billing_cycle_days} days · last
            settled {formatDate(group.last_settled_at)} · next due {formatDate(group.next_due_at)}
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
          <GroupSettingsDialog group={group} />
          <LedgerActionDialog groupId={groupId} currentBalance={group.balance} />
          <NewAccountDialog defaultGroupId={groupId} />
          <SettleGroupDialog groupId={groupId} />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card>
          <CardContent className="p-4">
            <div className="flex items-center gap-1 text-xs text-muted-foreground">
              Usage this cycle
              <Tooltip>
                <TooltipTrigger asChild>
                  <Info className="h-3 w-3 cursor-help" />
                </TooltipTrigger>
                <TooltipContent className="max-w-xs">
                  Usage accrued since the last settle (drives the Pending amount), not Marzban's own per-account
                  counter shown in the table below — those can differ if Marzban has reset an account's quota since.
                </TooltipContent>
              </Tooltip>
            </div>
            <p className="text-lg font-semibold tabular-nums">
              {(group.current_cycle_used_bytes / 1024 ** 3).toFixed(2)} GB
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Pending (not yet charged)</p>
            <p className="text-lg font-semibold tabular-nums text-warning">{formatToman(group.pending_amount)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Settled balance</p>
            <p className="text-lg font-semibold tabular-nums">{formatToman(group.balance)}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4">
            <p className="text-xs text-muted-foreground">Members</p>
            <p className="text-lg font-semibold tabular-nums">{group.account_count}</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle className="text-base">Member accounts ({accountsQuery.data?.length ?? 0})</CardTitle>
          <Button size="sm" variant="outline" className="gap-1.5" onClick={copySummary} disabled={copying}>
            <Copy className="h-3.5 w-3.5" /> {copying ? 'Copying…' : 'Copy usage summary'}
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Username</TableHead>
                <TableHead>Usage (Marzban)</TableHead>
                <TableHead>
                  <div className="flex items-center gap-1">
                    Billable this cycle
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Info className="h-3 w-3 cursor-help" />
                      </TooltipTrigger>
                      <TooltipContent className="max-w-xs">
                        Usage since the group's last settle, at this account's effective rate — this is what feeds
                        the Pending amount above. Can differ from "Usage" if Marzban has reset this account's quota
                        since the last settle.
                      </TooltipContent>
                    </Tooltip>
                  </div>
                </TableHead>
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
              {accountsQuery.data?.map((a) => {
                const billable = billableByAccount.get(a.id)
                return (
                  <TableRow key={a.id}>
                    <TableCell className="font-mono">
                      <Link to={`/accounts?highlight=${a.id}`} className="hover:underline">
                        {a.marzban_username}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <UsageBar used={a.used_traffic} limit={a.data_limit} />
                    </TableCell>
                    <TableCell className="text-sm">
                      {billable ? (
                        <div className="flex flex-col">
                          <span className="font-mono tabular-nums">{billable.billable_gb} GB</span>
                          <span className="text-xs text-muted-foreground">{formatToman(billable.amount)}</span>
                        </div>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell>{formatDate(a.expire)}</TableCell>
                    <TableCell>
                      <Badge variant={a.status === 'active' ? 'success' : 'outline'}>{a.status ?? 'unknown'}</Badge>
                    </TableCell>
                    <TableCell className="flex flex-wrap justify-end gap-2">
                      <AdjustAccountDialog account={a} groupRatePerGb={group.rate_per_gb} />
                      <ResetUsageDialog account={a} />
                      <BillingDialog account={a} groupRatePerGb={group.rate_per_gb} />
                      <RelationshipDialog account={a} />
                    </TableCell>
                  </TableRow>
                )
              })}
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
