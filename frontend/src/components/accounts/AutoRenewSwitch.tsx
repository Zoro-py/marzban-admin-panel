import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, apiErrorMessage } from '@/lib/api'
import { cn } from '@/lib/utils'

/** Per-account auto-renew toggle, usable straight from the accounts table
 * row (2026-09-29 checklist area 2.7 — «ساده‌سازی»): turning ON is immediate
 * (the safe direction — it only re-enters an account into the queue it is
 * excluded from), turning OFF asks first, because it silently stops a
 * customer's automatic renewals — exactly the kind of decision an operator
 * must not do by mis-tap. Same PATCH /billing the inspector's BillingSection
 * saves, with only this field in the body; the backend records the change in
 * the billing AccountEvent's audit line. */
export function AutoRenewSwitch({
  accountId,
  username,
  enabled,
  className,
}: {
  accountId: number
  username: string
  enabled: boolean
  className?: string
}) {
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: async (next: boolean) => {
      await accountsApi.updateBilling(accountId, { auto_renew_enabled: next })
      return next
    },
    onSuccess: (next) => {
      toast.success(next ? `Auto-renew on for ${username}` : `Auto-renew off for ${username}`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  function toggle() {
    if (enabled && !window.confirm(`Turn auto-renew OFF for ${username}?\nThey will no longer be queued for automatic renewal.`)) {
      return
    }
    mutation.mutate(!enabled)
  }

  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      aria-label={`Auto-renew for ${username}: ${enabled ? 'on' : 'off'}`}
      disabled={mutation.isPending}
      onClick={(e) => {
        // The whole row opens the inspector — the switch must not.
        e.stopPropagation()
        toggle()
      }}
      title={enabled ? 'Auto-renew on — click to turn off' : 'Auto-renew off — click to turn on'}
      className={cn(
        'relative inline-flex h-4.5 w-8 shrink-0 items-center rounded-full border transition-colors disabled:opacity-50',
        enabled ? 'border-primary/50 bg-primary/80' : 'border-border bg-muted',
        className,
      )}
    >
      <span
        className={cn(
          'inline-block h-3.5 w-3.5 transform rounded-full bg-background shadow transition-transform',
          enabled ? 'translate-x-4' : 'translate-x-0.5',
        )}
      />
    </button>
  )
}
