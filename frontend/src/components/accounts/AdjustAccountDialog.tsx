import * as React from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, ledgerApi, apiErrorMessage } from '@/lib/api'
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
import { formatBytes, formatDate, formatToman } from '@/lib/utils'
import { CalendarClock } from 'lucide-react'

const DAY_PRESETS = [-30, -7, 7, 30]

interface AdjustAccountDialogProps {
  // Callers that already have the enriched AccountRow (server-resolved through
  // the full account -> group -> dashboard-default chain) should pass it as-is
  // — effective_rate is used directly when present. Callers with only a plain
  // Account (customers.py/groups.py's older, unenriched account lists) fall
  // back to the account/group-only computation via groupRatePerGb.
  account: Account & { effective_rate?: number }
  groupRatePerGb?: number | null
  trigger?: React.ReactNode
}

export function AdjustAccountDialog({ account, groupRatePerGb, trigger }: AdjustAccountDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [extendDays, setExtendDays] = React.useState('')
  const [extendGb, setExtendGb] = React.useState('')
  const [note, setNote] = React.useState('')
  const [recordCharge, setRecordCharge] = React.useState(true)
  const [chargeAmount, setChargeAmount] = React.useState('')
  const [chargeTouched, setChargeTouched] = React.useState(false)
  const queryClient = useQueryClient()

  const effectiveRate =
    account.effective_rate && account.effective_rate > 0
      ? account.effective_rate
      : (account.rate_per_gb ?? groupRatePerGb ?? null)
  const canBill = !!(account.customer_id || account.group_id)
  const suggestedCharge = effectiveRate && extendGb && Number(extendGb) > 0 ? Number(extendGb) * effectiveRate : 0

  React.useEffect(() => {
    if (!chargeTouched) setChargeAmount(suggestedCharge > 0 ? String(Math.round(suggestedCharge)) : '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [suggestedCharge])

  const mutation = useMutation({
    mutationFn: async () => {
      const updated = await accountsApi.adjust(account.id, {
        extend_days: extendDays ? Number(extendDays) : undefined,
        extend_gb: extendGb ? Number(extendGb) : undefined,
        note: note || undefined,
      })
      if (recordCharge && canBill && chargeAmount && Number(chargeAmount) > 0) {
        await ledgerApi.create({
          type: 'charge',
          amount: Number(chargeAmount),
          customer_id: account.customer_id ?? undefined,
          group_id: account.group_id ?? undefined,
          account_id: account.id,
          note: note || `+${extendGb}GB for ${account.marzban_username}`,
        })
      }
      return updated
    },
    onSuccess: () => {
      toast.success(`Updated ${account.marzban_username}`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      setOpen(false)
      setExtendDays('')
      setExtendGb('')
      setNote('')
      setChargeAmount('')
      setChargeTouched(false)
      setRecordCharge(true)
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

          {Number(extendGb) > 0 && (
            <div className="rounded-lg border border-border bg-muted/40 p-3">
              <label className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  className="mt-1"
                  checked={recordCharge}
                  disabled={!canBill}
                  onChange={(e) => setRecordCharge(e.target.checked)}
                />
                <span className="flex-1">
                  {canBill ? (
                    <>
                      Also record a debt for this data
                      {effectiveRate ? (
                        <span className="text-muted-foreground"> (suggested: {extendGb} GB × {formatToman(effectiveRate)}/GB)</span>
                      ) : (
                        <span className="text-muted-foreground"> — no rate set, enter an amount manually</span>
                      )}
                    </>
                  ) : (
                    <span className="text-muted-foreground">Assign this account to a customer to bill this data</span>
                  )}
                </span>
              </label>
              {recordCharge && canBill && (
                <Input
                  type="number"
                  min={0}
                  className="mt-2"
                  value={chargeAmount}
                  onChange={(e) => {
                    setChargeTouched(true)
                    setChargeAmount(e.target.value)
                  }}
                  placeholder="Amount in Toman"
                />
              )}
            </div>
          )}

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
