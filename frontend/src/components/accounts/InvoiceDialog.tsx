import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ledgerApi, apiErrorMessage } from '@/lib/api'
import type { AccountRow, LedgerType } from '@/lib/types'
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { formatToman } from '@/lib/utils'
import { Receipt } from 'lucide-react'

interface InvoiceDialogProps {
  account: AccountRow
  trigger?: React.ReactNode
}

/** Item 3 of the UI ask: add debt/credit to an account via an explicit
 * volume (GB) × price (Toman/GB) × period invoice, not a bare amount field —
 * so the resulting ledger note is self-explanatory later without having to
 * remember what a raw number meant.
 *
 * Posts with account.customer_id/account.group_id exactly as-is (never
 * resolving a group's representative customer as a customer_id fallback) —
 * matching the convention every other per-account money action in this
 * codebase already uses (accounts.py's reset_account, AdjustAccountDialog).
 * This matters concretely: the balance badge shown on this exact row is
 * computed by summing ledger entries tagged with THIS account's group_id for
 * grouped accounts — an entry that only carried customer_id would never show
 * up there, making the charge look like it silently failed. */
export function InvoiceDialog({ account, trigger }: InvoiceDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [type, setType] = React.useState<LedgerType>('charge')
  const [volumeGb, setVolumeGb] = React.useState('')
  const [price, setPrice] = React.useState(account.effective_rate ? String(account.effective_rate) : '')
  const [period, setPeriod] = React.useState('')
  const [note, setNote] = React.useState('')
  const queryClient = useQueryClient()

  const canBill = !!(account.customer_id || account.group_id)
  const amount = (Number(volumeGb) || 0) * (Number(price) || 0)

  const mutation = useMutation({
    mutationFn: () => {
      if (!canBill) throw new Error('This account has no customer or group to bill — assign one first')
      const parts = [`${volumeGb || 0} GB × ${formatToman(Number(price) || 0)}/GB`]
      if (period) parts.push(`for ${period}`)
      if (note) parts.push(`— ${note}`)
      return ledgerApi.create({
        type,
        amount: Math.round(amount * 100) / 100,
        customer_id: account.customer_id ?? undefined,
        group_id: account.group_id ?? undefined,
        account_id: account.id,
        note: parts.join(' '),
      })
    },
    onSuccess: () => {
      toast.success(`${type === 'charge' ? 'Charged' : 'Credited'} ${formatToman(amount)} for ${account.marzban_username}`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
      setVolumeGb('')
      setPeriod('')
      setNote('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" variant="outline" className="gap-1.5">
            <Receipt className="h-3.5 w-3.5" /> Invoice
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="font-mono">{account.marzban_username}</DialogTitle>
          <DialogDescription>
            {canBill
              ? 'Record a charge (debt) or credit (payment) for this account, priced by volume and period.'
              : "This account isn't assigned to a customer or group yet — assign one before invoicing it."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label>Type</Label>
            <Select value={type} onValueChange={(v) => setType(v as LedgerType)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="charge">Charge (adds debt — بدهی)</SelectItem>
                <SelectItem value="credit">Credit (payment received — طلب)</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="invoice-volume">Volume (GB)</Label>
              <Input
                id="invoice-volume"
                type="number"
                min={0}
                value={volumeGb}
                onChange={(e) => setVolumeGb(e.target.value)}
                placeholder="0"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="invoice-price">Price (Toman/GB)</Label>
              <Input
                id="invoice-price"
                type="number"
                min={0}
                value={price}
                onChange={(e) => setPrice(e.target.value)}
                placeholder="0"
              />
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="invoice-period">Period</Label>
            <Input
              id="invoice-period"
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
              placeholder="e.g. Mordad 1405 / Aug 2026"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="invoice-note">Note (optional)</Label>
            <Input id="invoice-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="" />
          </div>

          <div className="flex items-center justify-between rounded-lg border border-border bg-muted/40 p-3 text-sm">
            <span className="text-muted-foreground">Amount</span>
            <span className="font-semibold tabular-nums">{formatToman(amount)}</span>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending || !canBill || amount <= 0}
          >
            {mutation.isPending ? 'Saving…' : `${type === 'charge' ? 'Add charge' : 'Add credit'} — ${formatToman(amount)}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
