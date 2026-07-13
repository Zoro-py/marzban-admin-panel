import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { customersApi, groupsApi, apiErrorMessage } from '@/lib/api'
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
import { Building2 } from 'lucide-react'

export function NewGroupDialog() {
  const [open, setOpen] = React.useState(false)
  const [name, setName] = React.useState('')
  const [repId, setRepId] = React.useState<string>('')
  const [rate, setRate] = React.useState('')
  const [cycleDays, setCycleDays] = React.useState('30')
  const queryClient = useQueryClient()

  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list, enabled: open })

  const mutation = useMutation({
    mutationFn: () =>
      groupsApi.create({
        name: name.trim(),
        representative_customer_id: Number(repId),
        billing_cycle_days: Number(cycleDays) || 30,
        rate_per_gb: rate ? Number(rate) : undefined,
      }),
    onSuccess: (group) => {
      toast.success(`Created group ${group.name}`)
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      setOpen(false)
      setName('')
      setRepId('')
      setRate('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" className="gap-1.5">
          <Building2 className="h-4 w-4" /> New group
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New pay-as-you-go group</DialogTitle>
          <DialogDescription>e.g. a company owner who settles usage for all employee accounts monthly.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="group-name">Group name</Label>
            <Input id="group-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Acme Co" />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Representative customer (billing contact)</Label>
            <Select value={repId} onValueChange={setRepId}>
              <SelectTrigger>
                <SelectValue placeholder="Select a customer" />
              </SelectTrigger>
              <SelectContent>
                {customersQuery.data?.map((c) => (
                  <SelectItem key={c.id} value={String(c.id)}>
                    {c.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="rate">Rate (Toman/GB)</Label>
              <Input id="rate" type="number" value={rate} onChange={(e) => setRate(e.target.value)} placeholder="20000" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="cycle">Billing cycle (days)</Label>
              <Input id="cycle" type="number" value={cycleDays} onChange={(e) => setCycleDays(e.target.value)} />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!name.trim() || !repId || mutation.isPending}>
            {mutation.isPending ? 'Saving…' : 'Create'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
