import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, apiErrorMessage } from '@/lib/api'
import type { Account } from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { formatBytes, formatToman } from '@/lib/utils'
import { RotateCcw } from 'lucide-react'

interface ResetUsageDialogProps {
  account: Account
  trigger?: React.ReactNode
}

export function ResetUsageDialog({ account, trigger }: ResetUsageDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [chargeAmount, setChargeAmount] = React.useState('')
  const [note, setNote] = React.useState('')
  const [prefilled, setPrefilled] = React.useState(false)
  const queryClient = useQueryClient()
  const isPayg = account.billing_mode === 'payg'

  const invoiceQuery = useQuery({
    queryKey: ['accounts', account.id, 'invoice'],
    queryFn: () => accountsApi.invoice(account.id),
    enabled: open && isPayg,
  })

  React.useEffect(() => {
    if (invoiceQuery.data && !prefilled) {
      setChargeAmount(invoiceQuery.data.amount > 0 ? String(invoiceQuery.data.amount) : '')
      setPrefilled(true)
    }
  }, [invoiceQuery.data, prefilled])

  const mutation = useMutation({
    mutationFn: () =>
      accountsApi.reset(account.id, {
        charge_amount: chargeAmount ? Number(chargeAmount) : undefined,
        note: note || undefined,
      }),
    onSuccess: () => {
      toast.success(`Reset ${account.marzban_username}${chargeAmount ? ` — charged ${formatToman(Number(chargeAmount))}` : ''}`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
      setChargeAmount('')
      setNote('')
      setPrefilled(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" variant="outline" className="gap-1.5">
            <RotateCcw className="h-3.5 w-3.5" /> Reset
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="font-mono">{account.marzban_username}</DialogTitle>
          <DialogDescription>
            Starts a new usage cycle in Marzban (used traffic back to 0).{' '}
            <Badge variant={isPayg ? 'warning' : 'outline'} className="ml-1">
              {isPayg ? 'pay-as-you-go' : 'prepay'}
            </Badge>
          </DialogDescription>
        </DialogHeader>

        {isPayg && (
          <div className="rounded-lg border border-border bg-muted/40 p-3 text-sm">
            {invoiceQuery.isLoading ? (
              <span className="text-muted-foreground">Calculating usage since last settlement…</span>
            ) : invoiceQuery.data ? (
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">
                  Used {invoiceQuery.data.billable_gb} GB × {formatToman(invoiceQuery.data.rate_per_gb)}/GB
                </span>
                <span className="font-semibold tabular-nums">{formatToman(invoiceQuery.data.amount)}</span>
              </div>
            ) : null}
          </div>
        )}

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="charge-amount">
              {isPayg ? 'Charge to record (suggested above — edit or clear it)' : 'Charge to record (optional)'}
            </Label>
            <Input
              id="charge-amount"
              type="number"
              min={0}
              value={chargeAmount}
              onChange={(e) => setChargeAmount(e.target.value)}
              placeholder="0"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="reset-note">Note (optional)</Label>
            <Input id="reset-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="July cycle" />
          </div>
          <p className="text-xs text-muted-foreground">
            Current usage before reset: {formatBytes(account.used_traffic)} / {formatBytes(account.data_limit)}
          </p>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
            {mutation.isPending ? 'Resetting…' : chargeAmount ? `Reset & charge ${formatToman(Number(chargeAmount))}` : 'Reset'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
