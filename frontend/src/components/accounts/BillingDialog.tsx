import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, apiErrorMessage } from '@/lib/api'
import type { Account, BillingMode } from '@/lib/types'
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
import { Tag } from 'lucide-react'

interface BillingDialogProps {
  account: Account
  groupRatePerGb?: number | null
  trigger?: React.ReactNode
}

export function BillingDialog({ account, groupRatePerGb, trigger }: BillingDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [rate, setRate] = React.useState(account.rate_per_gb != null ? String(account.rate_per_gb) : '')
  const [billingMode, setBillingMode] = React.useState<BillingMode>(account.billing_mode)
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: () =>
      accountsApi.updateBilling(account.id, {
        billing_mode: billingMode,
        ...(rate ? { rate_per_gb: Number(rate) } : { clear_rate: true }),
      }),
    onSuccess: () => {
      toast.success(`Updated billing for ${account.marzban_username}`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" variant="outline" className="gap-1.5">
            <Tag className="h-3.5 w-3.5" /> Billing
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="font-mono">{account.marzban_username}</DialogTitle>
          <DialogDescription>
            {account.group_id
              ? `Part of a group${groupRatePerGb ? ` (group rate: ${groupRatePerGb.toLocaleString()} T/GB)` : ''} — set a rate here to override it for just this account (a discount or markup).`
              : "Standalone rate — used for this account's own pay-as-you-go invoice."}{' '}
            Leave blank to fall back to the group rate, then the dashboard-wide default if neither is set.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rate">Rate (Toman/GB) — blank to use the group's rate</Label>
            <Input id="rate" type="number" value={rate} onChange={(e) => setRate(e.target.value)} placeholder="e.g. 18000" />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Billing mode</Label>
            <Select value={billingMode} onValueChange={(v) => setBillingMode(v as BillingMode)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="prepay">Prepay — charged manually when a package is sold</SelectItem>
                <SelectItem value="payg">Pay-as-you-go — resetting usage suggests a charge for what was used</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={mutation.isPending}>
            {mutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
