import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ArrowLeft, Clock, Copy, Info } from 'lucide-react'
import { groupsApi, ledgerApi, apiErrorMessage } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
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
import { Money } from '@/components/Money'
import { cn, daysUntil, formatDate, formatToman } from '@/lib/utils'

const INVOICE_INCLUDE_GB_KEY = 'invoice-include-gb'

export function GroupDetailPage() {
  const { id } = useParams<{ id: string }>()
  const groupId = Number(id)
  const [copying, setCopying] = React.useState(false)
  // Persisted like the theme toggle (lib/theme.tsx) — a per-operator display
  // preference, not per-group data, so it should stick across groups and
  // browser sessions rather than resetting every time this page is opened.
  // Defaults on: most invoices are sent alongside "why do I owe this", and GB
  // is that answer.
  const [includeGb, setIncludeGb] = React.useState(() => localStorage.getItem(INVOICE_INCLUDE_GB_KEY) !== 'false')
  const openAccount = useOpenAccountInspector()

  React.useEffect(() => {
    localStorage.setItem(INVOICE_INCLUDE_GB_KEY, String(includeGb))
  }, [includeGb])

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

  /** The message the operator actually sends the group's lead to collect
   * money: who owes what right now, and the total. Written in Persian with
   * Persian numerals because it is pasted straight into a chat with the
   * customer, not read inside this panel.
   *
   * Deliberately per-member OWED, not per-member usage, as the PRIMARY
   * figure: usage is what an amount is derived from, not what anyone is
   * being asked to pay, and once someone has partly paid the two stop
   * matching (48.8 GB of usage next to "13,916 owed" reads as an error if
   * they're presented as equivalent). GB is opt-in (includeGb, persisted)
   * for when the operator wants "why do I owe this" alongside the amount —
   * shown as an explicitly labelled second figure, this cycle's usage, never
   * substituted for the owed amount, so it can't be read as one. Sourced
   * from the same billableByAccount the member table's own "Billable this
   * cycle" column uses, so the invoice can't disagree with the page it was
   * copied from.
   *
   * EVERY LINE LEADS WITH A PERSIAN WORD, DELIBERATELY — this is a plain-text
   * message with no formatting available, and usernames here are always
   * Latin. Per the Unicode bidi algorithm's own P2/P3 rule, a line's reading
   * direction is set by its first STRONG-directional character (bullets,
   * digits and punctuation are neutral and get skipped over); a line built
   * as "name: amount" has the Latin name as that first strong character and
   * silently reads as left-to-right, which is what actually made the earlier
   * version of this message unreadable except for the title/total lines
   * (which already happened to start with a Persian word). The amount now
   * comes BEFORE the name specifically so "تومان" is that first strong
   * character — this is the same mechanism that already made the title and
   * total lines correct, just applied on purpose everywhere instead of by
   * accident on two lines.
   *
   * A previous version of this fix used invisible Unicode bidi-isolate
   * control characters (RLI/LRI/PDI) instead of rewording. Do not go back to
   * that: it was confirmed in production to render as visible stray marks
   * around every name in at least one real client, which is a strictly worse
   * failure than the original bug — this plain reordering needs no Unicode
   * feature support from whatever the message eventually gets pasted into. */
  async function copyInvoice() {
    setCopying(true)
    try {
      const members = accountsQuery.data ?? []
      const toman = (n: number) => `${Math.round(n).toLocaleString('fa-IR')} تومان`
      const gbNote = (accountId: number) => {
        if (!includeGb) return ''
        const gb = billableByAccount.get(accountId)?.billable_gb
        if (!gb || gb <= 0) return ''
        return ` (${gb.toLocaleString('fa-IR', { maximumFractionDigits: 1 })} گیگ این دوره)`
      }

      const owing = members.filter((m) => m.net_owed > 0).sort((a, b) => b.net_owed - a.net_owed)
      const inCredit = members.filter((m) => m.net_owed < 0)
      const settledCount = members.length - owing.length - inCredit.length

      const body: string[] = owing.map((m) => `• ${toman(m.net_owed)}: ${m.marzban_username}${gbNote(m.id)}`)
      
      for (const m of inCredit) {
        body.push(`• طلبکار ${toman(Math.abs(m.net_owed))}: ${m.marzban_username}${gbNote(m.id)}`)
      }

      if (owing.length === 0 && inCredit.length === 0) {
        body.push('• همه اعضا تسویه هستند.')
      } else if (settledCount > 0) {
        body.push(`• ${settledCount.toLocaleString('fa-IR')} نفر تسویه‌شده`)
      }

      const text = [
        `صورتحساب گروه «${group.name}»`,
        `تاریخ: ${new Date().toLocaleDateString('fa-IR')}`,
        '',
        ...body,
        '',
        `جمع کل: ${toman(group.net_owed)}`,
      ].join('\n')

      await navigator.clipboard.writeText(text)
      toast.success('Invoice copied — ready to send')
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
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
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
        <div className="flex flex-wrap items-center gap-2">
          <GroupSettingsDialog group={group} />
          <LedgerActionDialog groupId={groupId} currentBalance={group.net_owed} />
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
        {/* ONE money figure, and it is exactly the sum of the Owes now column
            below (guaranteed server-side — see MoneyBook). Showing "pending"
            and "settled balance" as two separate cards is what let this page
            claim the group was 270,000 in credit while every member row under
            it still owed money. */}
        <StatCard
          label="Owes now"
          value={
            group.net_owed === 0
              ? 'settled'
              : `${formatToman(Math.abs(group.net_owed))}${group.net_owed < 0 ? ' cr' : ''}`
          }
          tone={group.net_owed > 0 ? 'destructive' : group.net_owed < 0 ? 'credit' : 'success'}
        />
        {/* Not a separate debt — it's the size of the charge Settle would post
            RIGHT NOW, and it's already folded into "Owes now" above (see
            MoneyBook: net = posted + pending). Coloring this warning whenever
            it's simply non-zero made a group that's actually well in credit
            (existing prepayment already covers this cycle's package/usage)
            show an alarming yellow number with no explanation — tone follows
            the real net position instead, and the hint always says what this
            figure actually is so it's never just an unexplained number. */}
        <StatCard
          label="Not invoiced yet"
          value={formatToman(group.pending_amount)}
          tone={group.net_owed > 0 ? 'warning' : 'default'}
          hint={
            group.pending_amount > 0
              ? group.net_owed > 0
                ? 'This cycle’s package/usage — already counted in "Owes now," not on top of it.'
                : 'Already covered by existing credit — see "Owes now."'
              : undefined
          }
        />
        <StatCard label="Members" value={group.account_count} />
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
          <h2 className="flex items-center gap-1.5 text-[13px] font-semibold">
            Member accounts
            <span className="text-xs font-normal tabular-nums text-muted-foreground">{accountsQuery.data?.length ?? 0}</span>
          </h2>
          <div className="flex items-center gap-3">
            <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
              <Checkbox checked={includeGb} onCheckedChange={(v) => setIncludeGb(v === true)} />
              Include GB
            </label>
            <Button size="sm" variant="outline" onClick={copyInvoice} disabled={copying}>
              <Copy /> {copying ? 'Copying…' : 'Copy invoice'}
            </Button>
          </div>
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              {/* Account and Owes now are the collection view and survive at
                  every width; usage detail is context and drops out first. */}
              <TableHead>Account</TableHead>
              <TableHead className="hidden sm:table-cell">Usage (Marzban)</TableHead>
              <TableHead className="hidden md:table-cell">
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
              <TableHead>
                <span className="flex items-center gap-1">
                  Owes now
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Info className="h-3 w-3 cursor-help" />
                    </TooltipTrigger>
                    <TooltipContent className="max-w-xs">
                      What this member owes right now — their own posted charges and payments, plus their share of
                      the usage above that hasn't been invoiced yet. Settling the whole group charges everyone
                      together; recording a payment on one account only affects that account.
                    </TooltipContent>
                  </Tooltip>
                </span>
              </TableHead>
              <TableHead className="hidden lg:table-cell">Expires</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {accountsQuery.data?.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
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
                  <TableCell className="hidden sm:table-cell">
                    <UsageBar used={a.used_traffic} limit={a.data_limit} compact />
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    {billable ? (
                      <span className="flex items-baseline gap-2">
                        <span className="font-mono text-xs tabular-nums">{billable.billable_gb} GB</span>
                        <span className="text-[11px] tabular-nums text-muted-foreground">{formatToman(billable.amount)}</span>
                      </span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right sm:text-left">
                    <Money amount={a.net_owed} zero="settled" className="text-xs" />
                  </TableCell>
                  <TableCell className="hidden lg:table-cell">
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
              <TableHead className="hidden md:table-cell">Note</TableHead>
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
                <TableCell className="hidden max-w-[320px] truncate text-muted-foreground md:table-cell" title={entry.note ?? undefined}>
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
