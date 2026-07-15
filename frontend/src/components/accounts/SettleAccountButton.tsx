import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { formatToman } from '@/lib/utils'
import { CheckCircle2 } from 'lucide-react'

/** One-click settle for a standalone account's current pending amount — no
 * manual GB/price entry (which rarely lands on a round number anyway).
 * Charges exactly what's already shown as pending and rolls the matching
 * baseline forward (usage for payg, package size for prepay). Reachable
 * directly from the Accounts table row, not buried in the inspector. */
export function SettleAccountButton({
  accountId,
  username,
  amount,
}: {
  accountId: number
  username: string
  amount: number
}) {
  const [open, setOpen] = React.useState(false)
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: () => accountsApi.settle(accountId),
    onSuccess: (result: { charged_amount: number }) => {
      toast.success(`Settled ${username} — charged ${formatToman(result.charged_amount)}`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <button
          type="button"
          onClick={(e) => e.stopPropagation()}
          className="flex items-center gap-1 text-[11px] font-medium text-success hover:underline"
        >
          <CheckCircle2 className="h-3 w-3" /> Settle
        </button>
      </DialogTrigger>
      <DialogContent onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>Settle {username}</DialogTitle>
          <DialogDescription>
            Charges {formatToman(amount)} — the same amount already shown as pending — and rolls this account's
            billing baseline forward. No manual GB/price entry needed.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
            {mutation.isPending ? 'Settling…' : `Confirm & charge ${formatToman(amount)}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
