import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Settings } from 'lucide-react'
import { settingsApi, apiErrorMessage } from '@/lib/api'
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

/** Item 1 of the UI ask: a place to set the dashboard-wide default rate. Every
 * account/group without its own rate falls back to this (see backend's
 * services.effective_rate) — so this one field is what makes "set a global
 * rate" actually reach every unrated account instead of only new ones. */
export function SettingsDialog() {
  const [open, setOpen] = React.useState(false)
  const [rate, setRate] = React.useState('')
  const queryClient = useQueryClient()

  const settingsQuery = useQuery({ queryKey: ['settings'], queryFn: settingsApi.get, enabled: open })

  React.useEffect(() => {
    if (settingsQuery.data) {
      setRate(settingsQuery.data.default_rate_per_gb != null ? String(settingsQuery.data.default_rate_per_gb) : '')
    }
  }, [settingsQuery.data])

  const mutation = useMutation({
    mutationFn: () => settingsApi.update({ default_rate_per_gb: rate ? Number(rate) : null }),
    onSuccess: () => {
      toast.success('Default rate updated')
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-xs font-medium text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
        >
          <Settings className="h-3.5 w-3.5" />
          Settings
        </button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Dashboard settings</DialogTitle>
          <DialogDescription>
            This rate applies to any account that has neither its own rate nor a group rate — the last stop in the
            fallback chain (account → group → this default).
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="default-rate">Default rate (Toman/GB)</Label>
          <Input
            id="default-rate"
            type="number"
            value={rate}
            onChange={(e) => setRate(e.target.value)}
            placeholder="e.g. 15000"
          />
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={mutation.isPending || settingsQuery.isLoading}>
            {mutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
