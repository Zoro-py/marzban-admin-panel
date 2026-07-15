import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Money } from '@/components/Money'
import { cn, formatToman } from '@/lib/utils'
import { CheckCircle2 } from 'lucide-react'

/** One-click settle for a standalone account's current pending amount — no
 * manual GB/price entry (which rarely lands on a round number anyway).
 *
 * "Settle" POSTS A CHARGE (the estimate becomes a real, formal debt) — it is
 * NOT, by itself, a record that payment was received, which is why a settled
 * account can still show red afterward if nobody also records the payment.
 * The "payment received now too" checkbox closes that gap for the common
 * case (cash/transfer collected on the spot): checked, it also posts a
 * matching credit in the same action, so the balance nets back to settled
 * instead of staying owed. The preview below always shows the resulting
 * color/state before confirming, so there's no surprise either way. */
export function SettleAccountButton({
  accountId,
  username,
  amount,
  currentBalance,
  trigger,
  disabled,
}: {
  accountId: number
  username: string
  amount: number
  currentBalance: number
  /** Custom trigger element — defaults to a small inline text link, styled
   * for a dense table cell. Pass a <Button> to match a button-row context. */
  trigger?: React.ReactNode
  disabled?: boolean
}) {
  const [open, setOpen] = React.useState(false)
  const [markPaid, setMarkPaid] = React.useState(true)
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: () => accountsApi.settle(accountId, { mark_paid: markPaid }),
    onSuccess: () => {
      toast.success(
        markPaid ? `Settled ${username} — paid in full` : `Settled ${username} — charged ${formatToman(amount)}, still owed`,
      )
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const resultingBalance = currentBalance + amount - (markPaid ? amount : 0)

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <button
            type="button"
            disabled={disabled}
            onClick={(e) => e.stopPropagation()}
            className="flex items-center gap-1 text-[11px] font-medium text-success hover:underline disabled:pointer-events-none disabled:opacity-50"
          >
            <CheckCircle2 className="h-3 w-3" /> Settle
          </button>
        )}
      </DialogTrigger>
      <DialogContent onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>Settle {username}</DialogTitle>
          <DialogDescription>
            Posts a charge of {formatToman(amount)} for the pending amount shown — this bills it, it does not by
            itself record a payment.
          </DialogDescription>
        </DialogHeader>

        <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-muted/40 p-2.5 text-xs">
          <Checkbox checked={markPaid} onCheckedChange={(v) => setMarkPaid(v === true)} className="mt-0.5" />
          <span className="flex-1">
            <span className="font-medium">Payment received now too</span>
            <p className="mt-0.5 text-muted-foreground">
              Check this if they're paying you right now — also posts a matching credit so the balance below ends
              up settled, not owed. Leave unchecked if you're billing them for later collection.
            </p>
          </span>
        </label>

        <div
          className={cn(
            'flex items-center justify-between rounded-md border p-2.5 text-xs',
            resultingBalance > 0 && 'border-destructive/30 bg-destructive/5',
            resultingBalance < 0 && 'border-credit/30 bg-credit/5',
            resultingBalance === 0 && 'border-success/30 bg-success/5',
          )}
        >
          <span className="text-muted-foreground">{username}'s balance after this:</span>
          <Money amount={resultingBalance} zero="settled" className="font-semibold" />
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
            {mutation.isPending ? 'Settling…' : `Confirm — charge ${formatToman(amount)}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
