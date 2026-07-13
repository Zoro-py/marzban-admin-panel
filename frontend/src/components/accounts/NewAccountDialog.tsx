import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, customersApi, groupsApi, apiErrorMessage } from '@/lib/api'
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
import { UserPlus } from 'lucide-react'

const NONE = '__none__'

interface NewAccountDialogProps {
  defaultCustomerId?: number
  defaultGroupId?: number
  trigger?: React.ReactNode
}

export function NewAccountDialog({ defaultCustomerId, defaultGroupId, trigger }: NewAccountDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [username, setUsername] = React.useState('')
  const [customerId, setCustomerId] = React.useState<string>(defaultCustomerId ? String(defaultCustomerId) : NONE)
  const [groupId, setGroupId] = React.useState<string>(defaultGroupId ? String(defaultGroupId) : NONE)
  const [expireDays, setExpireDays] = React.useState('30')
  const [dataLimitGb, setDataLimitGb] = React.useState('')
  const [ratePerGb, setRatePerGb] = React.useState('')
  const queryClient = useQueryClient()

  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list, enabled: open })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list, enabled: open })

  const mutation = useMutation({
    mutationFn: () =>
      accountsApi.create({
        marzban_username: username.trim(),
        customer_id: customerId === NONE ? null : Number(customerId),
        group_id: groupId === NONE ? null : Number(groupId),
        expire: expireDays ? Math.floor(Date.now() / 1000) + Number(expireDays) * 86400 : null,
        data_limit: dataLimitGb ? Math.round(Number(dataLimitGb) * 1024 ** 3) : null,
        rate_per_gb: ratePerGb ? Number(ratePerGb) : null,
      }),
    onSuccess: (account) => {
      toast.success(`Created ${account.marzban_username} in Marzban`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      setOpen(false)
      setUsername('')
      setDataLimitGb('')
      setRatePerGb('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" className="gap-1.5">
            <UserPlus className="h-4 w-4" /> New account
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create a new Marzban account</DialogTitle>
          <DialogDescription>This creates the user directly in Marzban, then tracks it here.</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="username">Marzban username</Label>
            <Input id="username" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="ali_family_2" autoFocus />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label>Customer</Label>
              <Select value={customerId} onValueChange={setCustomerId}>
                <SelectTrigger>
                  <SelectValue placeholder="Unassigned" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>Unassigned</SelectItem>
                  {customersQuery.data?.map((c) => (
                    <SelectItem key={c.id} value={String(c.id)}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>Group (pay-as-you-go)</Label>
              <Select value={groupId} onValueChange={setGroupId}>
                <SelectTrigger>
                  <SelectValue placeholder="None" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>None</SelectItem>
                  {groupsQuery.data?.map((g) => (
                    <SelectItem key={g.id} value={String(g.id)}>
                      {g.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="expire-days">Expires in (days, blank = never)</Label>
              <Input id="expire-days" type="number" value={expireDays} onChange={(e) => setExpireDays(e.target.value)} placeholder="30" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="data-limit">Data limit (GB, blank = unlimited)</Label>
              <Input id="data-limit" type="number" value={dataLimitGb} onChange={(e) => setDataLimitGb(e.target.value)} placeholder="50" />
            </div>
          </div>

          {groupId === NONE && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="rate">Standalone pay-as-you-go rate (Toman/GB, optional)</Label>
              <Input id="rate" type="number" value={ratePerGb} onChange={(e) => setRatePerGb(e.target.value)} placeholder="20000" />
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!username.trim() || mutation.isPending}>
            {mutation.isPending ? 'Creating…' : 'Create'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
