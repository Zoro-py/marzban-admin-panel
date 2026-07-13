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
import { CircleMinus, CirclePlus } from 'lucide-react'

interface LedgerActionDialogProps {
  customerId?: number
  groupId?: number
  trigger?: React.ReactNode
  defaultType?: LedgerType
}

export function LedgerActionDialog({ customerId, groupId, trigger, defaultType = 'charge' }: LedgerActionDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [type, setType] = React.useState<LedgerType>(defaultType)
  const [amount, setAmount] = React.useState('')
  const [note, setNote] = React.useState('')
  const queryClient = useQueryClient()

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
          <DialogDescription>Charges (debt owed to you) and credits (payments received) are both logged here.</DialogDescription>
        </DialogHeader>

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
            <Label htmlFor="amount">Amount (Toman)</Label>
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
