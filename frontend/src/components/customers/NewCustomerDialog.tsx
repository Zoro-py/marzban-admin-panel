import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { customersApi, apiErrorMessage } from '@/lib/api'
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
import { UserPlus } from 'lucide-react'

export function NewCustomerDialog() {
  const [open, setOpen] = React.useState(false)
  const [name, setName] = React.useState('')
  const [contact, setContact] = React.useState('')
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: () => customersApi.create({ name: name.trim(), contact: contact.trim() || undefined }),
    onSuccess: (customer) => {
      toast.success(`Added customer ${customer.name}`)
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      setOpen(false)
      setName('')
      setContact('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" className="gap-1.5">
          <UserPlus className="h-4 w-4" /> New customer
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New customer</DialogTitle>
          <DialogDescription>The billing contact — may end up owning several accounts.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="name">Name</Label>
            <Input id="name" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Ali Boojar" />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="contact">Contact (Telegram / phone)</Label>
            <Input id="contact" value={contact} onChange={(e) => setContact(e.target.value)} placeholder="@ali_boojar" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!name.trim() || mutation.isPending}>
            {mutation.isPending ? 'Saving…' : 'Create'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
