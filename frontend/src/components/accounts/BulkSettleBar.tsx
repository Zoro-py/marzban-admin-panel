import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, groupsApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Money } from '@/components/Money'
import type { AccountRow } from '@/lib/types'
import { CheckCircle2, X } from 'lucide-react'

/** Settle several accounts in one action, the daily-routine case this exists
 * for: "N customers paid today, record all of them" without opening each
 * one's own dialog. Deliberately a CLIENT-SIDE loop over the same
 * settle_account/settle_group_member endpoints the single-account button
 * already uses (sequential, not parallel — an operator watching the count
 * tick up is also a natural rate limit) rather than a new bulk endpoint:
 * every one of those money paths already carries its own test coverage, so
 * this reuses it instead of asking new backend logic to earn that trust
 * again from zero. See docs/DOMAIN_AND_BILLING.md before changing what a
 * single settle call here actually does. */
export function BulkSettleBar({
  selected,
  onClear,
}: {
  selected: AccountRow[]
  onClear: () => void
}) {
  const [markPaid, setMarkPaid] = React.useState(true)
  const queryClient = useQueryClient()

  const settleable = selected.filter((a) => a.pending_amount > 0)
  const total = settleable.reduce((sum, a) => sum + a.pending_amount, 0)

  const mutation = useMutation({
    mutationFn: async () => {
      const failed: { username: string; error: string }[] = []
      let settled = 0
      // Sequential on purpose: each account's settle reads its own
      // pre-charge balance from the DB, so racing several at once against
      // the same customer/group is exactly the concurrent-write shape
      // settle's own locking doesn't protect against by design (each call
      // is its own short transaction, not one big one across the batch).
      for (const a of settleable) {
        try {
          if (a.group_id) {
            await groupsApi.settleMember(a.group_id, a.id, { mark_paid: markPaid, pay_scope: 'full' })
          } else {
            await accountsApi.settle(a.id, { mark_paid: markPaid, pay_scope: 'full' })
          }
          settled += 1
        } catch (err) {
          failed.push({ username: a.marzban_username, error: apiErrorMessage(err) })
        }
      }
      return { settled, failed }
    },
    onSuccess: ({ settled, failed }) => {
      if (failed.length === 0) {
        toast.success(`Settled ${settled} account${settled === 1 ? '' : 's'}${markPaid ? ' — paid in full' : ''}`)
      } else {
        toast.warning(
          `Settled ${settled} of ${settled + failed.length} — failed: ${failed.map((f) => f.username).join(', ')}`,
        )
      }
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      onClear()
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-primary/30 bg-primary/5 px-3 py-2">
      <span className="text-xs font-medium">
        {selected.length} selected
        {settleable.length !== selected.length && (
          <span className="text-muted-foreground"> ({settleable.length} with something owed)</span>
        )}
      </span>

      {settleable.length > 0 && (
        <span className="text-xs text-muted-foreground">
          <Money amount={total} kind="plain" /> total
        </span>
      )}

      <label className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
        <Checkbox checked={markPaid} onCheckedChange={(v) => setMarkPaid(v === true)} />
        Mark paid too
      </label>

      <Button
        size="sm"
        className="gap-1.5"
        onClick={() => mutation.mutate()}
        disabled={settleable.length === 0 || mutation.isPending}
      >
        <CheckCircle2 className="h-3.5 w-3.5" />
        {mutation.isPending ? 'Settling…' : `Settle ${settleable.length}`}
      </Button>

      <Button size="sm" variant="ghost" className="gap-1.5 text-muted-foreground" onClick={onClear} disabled={mutation.isPending}>
        <X className="h-3.5 w-3.5" />
        Clear
      </Button>
    </div>
  )
}
