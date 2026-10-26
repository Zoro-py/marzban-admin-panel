import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, apiErrorMessage } from '@/lib/api'
import { formatDate } from '@/lib/utils'
import { cn } from '@/lib/utils'

/** «Reconciled» tag (owner 2026-10-27): green = this account's ledger was
 * checked against Marzban's real usage; grey = not verified yet, so the bot
 * shows the customer an «approximate» warning. One click toggles it (clearing
 * asks first). A label only - it never changes a money figure. */
export function AccountingSyncBadge({
  accountId,
  username,
  syncedAt,
  className,
}: {
  accountId: number
  username: string
  syncedAt: string | null
  className?: string
}) {
  const queryClient = useQueryClient()
  const synced = !!syncedAt
  const mutation = useMutation({
    mutationFn: (next: boolean) => accountsApi.setAccountingSynced(accountId, next),
    onSuccess: (_, next) => {
      toast.success(next ? `${username}: marked reconciled` : `${username}: reconciliation mark removed`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['account', accountId] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  function toggle() {
    if (synced && !window.confirm(`Remove the «reconciled» mark from ${username}?`)) return
    mutation.mutate(!synced)
  }

  return (
    <button
      type="button"
      disabled={mutation.isPending}
      onClick={(e) => {
        e.stopPropagation()
        toggle()
      }}
      title={
        synced
          ? `حساب‌کتاب با مصرف واقعی تطبیق داده شده (${formatDate(syncedAt!)}) — کلیک برای برداشتن`
          : 'حساب‌کتاب هنوز تطبیق داده نشده؛ در بات برای مشتری «تقریبی» نشان داده می‌شود — کلیک برای تأیید'
      }
      className={cn(
        'inline-flex shrink-0 items-center rounded px-1.5 py-0.5 text-[10px] font-medium disabled:opacity-50',
        synced ? 'bg-success/15 text-success' : 'bg-muted text-muted-foreground',
        className,
      )}
    >
      {synced ? '✓ synced' : 'unverified'}
    </button>
  )
}
