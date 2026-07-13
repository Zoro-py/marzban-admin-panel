import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
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
import { formatBytes, formatDate } from '@/lib/utils'
import { CalendarClock } from 'lucide-react'

const DAY_PRESETS = [-30, -7, 7, 30]

interface AdjustAccountDialogProps {
  account: Account
  trigger?: React.ReactNode
}

export function AdjustAccountDialog({ account, trigger }: AdjustAccountDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [extendDays, setExtendDays] = React.useState('')
  const [extendGb, setExtendGb] = React.useState('')
  const [note, setNote] = React.useState('')
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: () =>
      accountsApi.adjust(account.id, {
        extend_days: extendDays ? Number(extendDays) : undefined,
        extend_gb: extendGb ? Number(extendGb) : undefined,
        note: note || undefined,
      }),
    onSuccess: () => {
      toast.success(`Updated ${account.marzban_username}`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
      setExtendDays('')
      setExtendGb('')
      setNote('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const canSubmit = (!!extendDays && Number(extendDays) !== 0) || (!!extendGb && Number(extendGb) !== 0)

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" variant="outline" className="gap-1.5">
            <CalendarClock className="h-3.5 w-3.5" /> Adjust
          </Button>
        )}
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="font-mono">{account.marzban_username}</DialogTitle>
          <DialogDescription>
            Current expiry: {formatDate(account.expire)} · Current limit: {formatBytes(account.data_limit)} · Used:{' '}
            {formatBytes(account.used_traffic)}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="extend-days">Days to add (negative to reduce)</Label>
            <Input id="extend-days" type="number" value={extendDays} onChange={(e) => setExtendDays(e.target.value)} placeholder="30" />
            <div className="flex gap-1.5">
              {DAY_PRESETS.map((d) => (
                <Button key={d} type="button" size="sm" variant="secondary" onClick={() => setExtendDays(String(d))}>
                  {d > 0 ? `+${d}d` : `${d}d`}
                </Button>
              ))}
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="extend-gb">GB to add (negative to reduce)</Label>
            <Input id="extend-gb" type="number" value={extendGb} onChange={(e) => setExtendGb(e.target.value)} placeholder="10" />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="adjust-note">Note (optional, kept in this account's history)</Label>
            <Input id="adjust-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Renewal for July" />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!canSubmit || mutation.isPending}>
            {mutation.isPending ? 'Applying…' : 'Apply to Marzban'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
