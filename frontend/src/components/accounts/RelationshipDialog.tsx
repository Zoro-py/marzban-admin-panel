import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, customersApi, groupsApi, apiErrorMessage } from '@/lib/api'
import type { Account, AccountRole } from '@/lib/types'
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Label } from '@/components/ui/label'
import { Link2 } from 'lucide-react'

const NONE = '__none__'

interface RelationshipDialogProps {
  account: Account
  trigger?: React.ReactNode
}

export function RelationshipDialog({ account, trigger }: RelationshipDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [customerId, setCustomerId] = React.useState(account.customer_id ? String(account.customer_id) : NONE)
  const [groupId, setGroupId] = React.useState(account.group_id ? String(account.group_id) : NONE)
  const [role, setRole] = React.useState<AccountRole>(account.role)
  const queryClient = useQueryClient()

  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list, enabled: open })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list, enabled: open })

  const mutation = useMutation({
    mutationFn: () =>
      accountsApi.updateRelationship(account.id, {
        customer_id: customerId === NONE ? null : Number(customerId),
        group_id: groupId === NONE ? null : Number(groupId),
        role,
      }),
    onSuccess: () => {
      toast.success(`Updated ownership for ${account.marzban_username}`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
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
            <Link2 className="h-3.5 w-3.5" /> Assign
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="font-mono">{account.marzban_username}</DialogTitle>
          <DialogDescription>Who owns this account, and is it billed on its own or as part of a group?</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
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

          <div className="flex flex-col gap-1.5">
            <Label>Role</Label>
            <Select value={role} onValueChange={(v) => setRole(v as AccountRole)}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="primary">Primary (the billing contact)</SelectItem>
                <SelectItem value="sub">Sub-account (e.g. a family member)</SelectItem>
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
