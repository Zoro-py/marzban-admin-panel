import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ArrowLeft, Clock, Copy, Info } from 'lucide-react'
import { groupsApi, ledgerApi, apiErrorMessage } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { LedgerActionDialog } from '@/components/ledger/LedgerActionDialog'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { SettleGroupDialog } from '@/components/groups/SettleGroupDialog'
import { ResetGroupCycleDialog } from '@/components/groups/ResetGroupCycleDialog'
import { GroupSettingsDialog } from '@/components/groups/GroupSettingsDialog'
import { UsageBar } from '@/components/UsageBar'
import { StatusDot } from '@/components/StatusDot'
import { StatCard } from '@/components/StatCard'
import { cn, daysUntil, formatDate, formatToman } from '@/lib/utils'

export function GroupDetailPage() {
  const { id } = useParams<{ id: string }>()
  const groupId = Number(id)
  const [copying, setCopying] = React.useState(false)
  const openAccount = useOpenAccountInspector()

  const groupQuery = useQuery({ queryKey: ['groups', groupId], queryFn: () => groupsApi.get(groupId) })
  const accountsQuery = useQuery({ queryKey: ['accounts', { groupId }], queryFn: () => groupsApi.accounts(groupId) })
  const ledgerQuery = useQuery({ queryKey: ['ledger', { groupId }], queryFn: () => ledgerApi.list({ group_id: groupId }) })
  // Fetched eagerly so the member table shows the SAME billable-GB figure the
  // pending amount is computed from.
  const invoiceQuery = useQuery({ queryKey: ['groups', groupId, 'invoice'], queryFn: () => groupsApi.invoice(groupId) })
  const billableByAccount = React.useMemo(
    () => new Map(invoiceQuery.data?.lines.map((l) => [l.account_id, l])),
    [invoiceQuery.data],
  )

  if (groupQuery.isLoading || !groupQuery.data) {
    return <p className="text-xs text-muted-foreground">Loading…</p>
  }

  const group = groupQuery.data

  // One-click summary of what this cycle would charge, ready to paste into a
  // chat with the customer.
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
    <div className="flex flex-col gap-4">
      <Link to="/groups" className="flex w-fit items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> Groups
      </Link>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-semibold tracking-tight">{group.name}</h1>
            <Badge variant={group.billing_mode === 'payg' ? 'warning' : 'secondary'}>
              {group.billing_mode === 'payg' ? 'pay-as-you-go' : 'prepay'}
            </Badge>
            {group.is_due && group.billing_mode === 'payg' && (
              <Badge variant="destructive" className="gap-1">
                <Clock className="h-3 w-3" /> due for settlement
              </Badge>
            )}
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {group.rate_per_gb ? `${formatToman(group.rate_per_gb)}/GB` : 'No group rate'} · every{' '}
            {group.billing_cycle_days} days · last settled {formatDate(group.last_settled_at)} · next due{' '}
            {formatDate(group.next_due_at)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <GroupSettingsDialog group={group} />
          <LedgerActionDialog groupId={groupId} currentBalance={group.balance} />
          <NewAccountDialog defaultGroupId={groupId} />
          {group.billing_mode === 'payg' && <ResetGroupCycleDialog groupId={groupId} />}
          <SettleGroupDialog groupId={groupId} currentBalance={group.balance} />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard
          label="Usage this cycle"
          value={`${(group.current_cycle_used_bytes / 1024 ** 3).toFixed(2)} GB`}
        />
        <StatCard
          label="Pending (not yet charged)"
          value={formatToman(group.pending_amount)}
          tone={group.pending_amount > 0 ? 'warning' : 'default'}
        />
        <StatCard
          label="Settled balance"
          value={
            group.balance === 0 ? 'settled' : `${formatToman(Math.abs(group.balance))}${group.balance < 0 ? ' cr' : ''}`
          }
          tone={group.balance > 0 ? 'destructive' : group.balance < 0 ? 'credit' : 'success'}
        />
        <StatCard label="Members" value={group.account_count} />
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <h2 className="flex items-center gap-1.5 text-[13px] font-semibold">
            Member accounts
            <span className="text-xs font-normal tabular-nums text-muted-foreground">{accountsQuery.data?.length ?? 0}</span>
          </h2>
          <Button size="sm" variant="outline" onClick={copySummary} disabled={copying}>
            <Copy /> {copying ? 'Copying…' : 'Copy usage summary'}
          </Button>
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Account</TableHead>
              <TableHead>Usage (Marzban)</TableHead>
              <TableHead>
                <span className="flex items-center gap-1">
                  Billable this cycle
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Info className="h-3 w-3 cursor-help" />
                    </TooltipTrigger>
                    <TooltipContent className="max-w-xs">
                      Usage since the group's last settle, at this account's effective rate — this feeds the Pending
                      figure above. Can differ from "Usage" if Marzban has reset this account's quota since.
                    </TooltipContent>
                  </Tooltip>
                </span>
              </TableHead>
              <TableHead>Expires</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {accountsQuery.data?.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                  No accounts in this group yet.
                </TableCell>
              </TableRow>
            )}
            {accountsQuery.data?.map((a) => {
              const billable = billableByAccount.get(a.id)
              const days = daysUntil(a.expire)
              return (
                <TableRow key={a.id} className="cursor-pointer" onClick={() => openAccount(a.id)}>
                  <TableCell>
                    <span className="flex items-center gap-2">
                      <StatusDot status={a.status} />
                      <span className="font-mono text-xs font-medium">{a.marzban_username}</span>
                    </span>
                  </TableCell>
                  <TableCell>
                    <UsageBar used={a.used_traffic} limit={a.data_limit} compact />
                  </TableCell>
                  <TableCell>
                    {billable ? (
                      <span className="flex items-baseline gap-2">
                        <span className="font-mono text-xs tabular-nums">{billable.billable_gb} GB</span>
                        <span className="text-[11px] tabular-nums text-muted-foreground">{formatToman(billable.amount)}</span>
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
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
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="border-b border-border px-4 py-2.5">
          <h2 className="text-[13px] font-semibold">Settlement history</h2>
        </div>
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
                <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                  No transactions yet.
                </TableCell>
              </TableRow>
            )}
            {ledgerQuery.data?.map((entry) => (
              <TableRow key={entry.id}>
                <TableCell className="text-xs text-muted-foreground">{formatDate(entry.date)}</TableCell>
                <TableCell>
                  <Badge variant={entry.type === 'charge' ? 'destructive' : 'success'}>
                    {entry.type === 'charge' ? 'debt' : 'payment'}
                  </Badge>
                </TableCell>
                <TableCell className="max-w-[320px] truncate text-muted-foreground" title={entry.note ?? undefined}>
                  {entry.note ?? '—'}
                </TableCell>
                <TableCell className="text-right">
                  <span
                    className={cn(
                      'text-xs font-medium tabular-nums',
                      entry.type === 'charge' ? 'text-destructive' : 'text-success',
                    )}
                  >
                    {entry.type === 'charge' ? '+' : '−'}
                    {Math.round(entry.amount).toLocaleString('en-US')} T
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
