import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { DatabaseBackup, Settings } from 'lucide-react'
import { settingsApi, backupApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Separator } from '@/components/ui/separator'
import { formatBytes, formatToman } from '@/lib/utils'
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
  // Audit trail for THIS dialog's own field: the dashboard-wide default's
  // change history, so "what was it before?" never needs the database.
  const rateHistoryQuery = useQuery({
    queryKey: ['settings', 'rate-changes', 'default'],
    queryFn: () => settingsApi.rateChanges(),
    enabled: open,
  })

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

  // Same pipeline as the nightly schedule — here so the operator can verify
  // the whole chain (DB copy, zip, Telegram delivery) works right after
  // setting BOT_TOKEN up, instead of discovering a silent failure at 3:30am.
  const backupMutation = useMutation({
    mutationFn: backupApi.run,
    onSuccess: (result) => {
      toast.success(`Backup sent to the admin chat: ${result.filename} (${formatBytes(result.size_bytes)})`)
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
            value={rate || ''}
            onChange={(e) => setRate(e.target.value)}
            placeholder="e.g. 15000"
          />
          {(rateHistoryQuery.data?.length ?? 0) > 0 && (
            <div className="rounded-md border border-border bg-background/50 p-2">
              <p className="text-[11px] font-medium text-muted-foreground">Change history</p>
              <ul className="mt-1 flex flex-col gap-0.5 text-[11px] text-muted-foreground">
                {rateHistoryQuery.data!.slice(0, 5).map((rc) => (
                  <li key={rc.id} className="tabular-nums">
                    {rc.old_rate != null ? formatToman(rc.old_rate) : 'unset'} → {rc.new_rate != null ? formatToman(rc.new_rate) : 'unset'}
                    {rc.created_by && <span className="ml-1">· {rc.created_by}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>

        <Separator />

        <div className="flex flex-col gap-1.5">
          <Label>Database backup</Label>
          <p className="text-xs text-muted-foreground">
            Runs the same backup the nightly schedule runs — a zip of the live database delivered to the admin Telegram
            chat. Use it to confirm the pipeline works now, not at 3:30am.
          </p>
          <div>
            <Button
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={() => backupMutation.mutate()}
              disabled={backupMutation.isPending}
            >
              <DatabaseBackup className="h-3.5 w-3.5" />
              {backupMutation.isPending ? 'Backing up…' : 'Back up now'}
            </Button>
          </div>
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
