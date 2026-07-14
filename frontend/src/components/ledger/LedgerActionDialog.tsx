import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ledgerApi, apiErrorMessage } from '@/lib/api'
import type { LedgerType } from '@/lib/types'
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
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { formatToman } from '@/lib/utils'
import { CircleMinus, CirclePlus } from 'lucide-react'

interface LedgerActionDialogProps {
  customerId?: number
  groupId?: number
  trigger?: React.ReactNode
  defaultType?: LedgerType
  // Current balance (positive = owed to us, negative = credit owed back) —
  // when given, the dialog can show "pay in full" and a live preview of what
  // recording this amount would leave as the new balance. Covers item 1 of
  // the follow-up feedback in full: partial payment (any amount less than the
  // balance), adding to debt (Debt tab), and recording an overpayment that
  // leaves a credit owed back (Credit tab, amount > current balance) are all
  // just this one form — the gap was that none of that was ever explained.
  currentBalance?: number
}

export function LedgerActionDialog({ customerId, groupId, trigger, defaultType = 'charge', currentBalance }: LedgerActionDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [type, setType] = React.useState<LedgerType>(defaultType)
  const [amount, setAmount] = React.useState('')
  const [note, setNote] = React.useState('')
  const queryClient = useQueryClient()

  React.useEffect(() => {
    if (open) {
      setType(defaultType)
      setAmount('')
      setNote('')
    }
  }, [open, defaultType])

  const mutation = useMutation({
    mutationFn: () =>
      ledgerApi.create({
        type,
        amount: Number(amount),
        customer_id: customerId,
        group_id: groupId,
        note: note || undefined,
      }),
    onSuccess: () => {
      toast.success(type === 'charge' ? 'Debt recorded' : 'Credit recorded')
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
      setAmount('')
      setNote('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" variant="outline">
            New debt / credit
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Record a transaction</DialogTitle>
          <DialogDescription>
            Debt = they owe you more (a new charge). Credit = money received — enter less than the full balance for
            a partial payment, the exact balance to settle it, or more than the balance to record an overpayment
            (leaves a credit owed back to them).
          </DialogDescription>
        </DialogHeader>

        {currentBalance !== undefined && (
          <div className="rounded-lg border border-border bg-muted/40 p-3 text-sm">
            <span className="text-muted-foreground">Current balance: </span>
            {currentBalance === 0 ? (
              <span className="font-medium">settled</span>
            ) : currentBalance > 0 ? (
              <span className="font-medium text-destructive">{formatToman(currentBalance)} owed to you</span>
            ) : (
              <span className="font-medium text-warning">{formatToman(Math.abs(currentBalance))} credit owed back to them</span>
            )}
          </div>
        )}

        <Tabs value={type} onValueChange={(v) => setType(v as LedgerType)}>
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="charge" className="gap-1.5">
              <CircleMinus className="h-4 w-4" /> Debt (بدهی)
            </TabsTrigger>
            <TabsTrigger value="credit" className="gap-1.5">
              <CirclePlus className="h-4 w-4" /> Credit (طلب)
            </TabsTrigger>
          </TabsList>
        </Tabs>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <Label htmlFor="amount">Amount (Toman)</Label>
              {type === 'credit' && currentBalance !== undefined && currentBalance > 0 && (
                <button
                  type="button"
                  className="text-xs text-primary hover:underline"
                  onClick={() => setAmount(String(currentBalance))}
                >
                  Pay in full ({formatToman(currentBalance)})
                </button>
              )}
            </div>
            <Input
              id="amount"
              type="number"
              min={0}
              autoFocus
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder="150000"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="note">Note (optional)</Label>
            <Input id="note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="1 month renewal" />
          </div>

          {currentBalance !== undefined && amount && Number(amount) > 0 && (
            <p className="text-xs text-muted-foreground">
              New balance after this:{' '}
              {(() => {
                const next = currentBalance + (type === 'charge' ? Number(amount) : -Number(amount))
                if (next === 0) return <span className="font-medium">settled</span>
                if (next > 0) return <span className="font-medium">{formatToman(next)} still owed</span>
                return <span className="font-medium">{formatToman(Math.abs(next))} credit owed back to them</span>
              })()}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!amount || Number(amount) <= 0 || mutation.isPending}>
            {mutation.isPending ? 'Saving…' : type === 'charge' ? 'Record debt' : 'Record credit'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
