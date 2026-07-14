import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { groupsApi, apiErrorMessage } from '@/lib/api'
import type { BillingMode, GroupWithBalance } from '@/lib/types'
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
import { Settings } from 'lucide-react'

interface GroupSettingsDialogProps {
  group: GroupWithBalance
  trigger?: React.ReactNode
}

/** Item 2 of the follow-up feedback: there was no way to change a group's
 * billing mode (or rate/cycle) after creating it — this is that missing
 * "edit" surface, mirroring accounts' BillingDialog. */
export function GroupSettingsDialog({ group, trigger }: GroupSettingsDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [name, setName] = React.useState(group.name)
  const [rate, setRate] = React.useState(group.rate_per_gb != null ? String(group.rate_per_gb) : '')
  const [cycleDays, setCycleDays] = React.useState(String(group.billing_cycle_days))
  const [billingMode, setBillingMode] = React.useState<BillingMode>(group.billing_mode)
  const queryClient = useQueryClient()

  React.useEffect(() => {
    if (open) {
      setName(group.name)
      setRate(group.rate_per_gb != null ? String(group.rate_per_gb) : '')
      setCycleDays(String(group.billing_cycle_days))
      setBillingMode(group.billing_mode)
    }
  }, [open, group])

  const mutation = useMutation({
    mutationFn: () =>
      groupsApi.update(group.id, {
        name: name.trim(),
        rate_per_gb: rate ? Number(rate) : undefined,
        billing_cycle_days: Number(cycleDays) || 30,
        billing_mode: billingMode,
      }),
    onSuccess: () => {
      toast.success(`Updated ${group.name}`)
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
            <Settings className="h-3.5 w-3.5" /> Settings
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{group.name} — settings</DialogTitle>
          <DialogDescription>Rate, billing cycle, and billing mode for this group.</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="group-settings-name">Group name</Label>
            <Input id="group-settings-name" value={name} onChange={(e) => setName(e.target.value)} />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label>Billing mode</Label>
            <Select value={billingMode} onValueChange={(v) => setBillingMode(v as BillingMode)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="payg">Pay-as-you-go — settle charges actual metered usage</SelectItem>
                <SelectItem value="prepay">Prepay — billed manually (a package sold up front)</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="group-settings-rate">Rate (Toman/GB)</Label>
              <Input
                id="group-settings-rate"
                type="number"
                value={rate}
                onChange={(e) => setRate(e.target.value)}
                placeholder="e.g. 20000"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="group-settings-cycle">Billing cycle (days)</Label>
              <Input id="group-settings-cycle" type="number" value={cycleDays} onChange={(e) => setCycleDays(e.target.value)} />
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!name.trim() || mutation.isPending}>
            {mutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
