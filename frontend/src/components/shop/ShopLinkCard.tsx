import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Clock, Link2, MessageCircle, ShieldCheck } from 'lucide-react'
import { apiErrorMessage, shopApi } from '@/lib/api'
import type { ShopLinkState } from '@/lib/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { formatDate } from '@/lib/utils'

// Same inline copy flow as every other surface (no shared clipboard helper
// exists — see DelegatesPage's copyInviteLink).
function copyInviteLink(text: string) {
  navigator.clipboard?.writeText(text).then(
    () => toast.success('Invite link copied — send it to the customer'),
    () => toast.error('Could not copy — select the link and copy it manually'),
  )
}

function PendingBody({ state }: { state: ShopLinkState }) {
  const queryClient = useQueryClient()
  const discard = useMutation({
    mutationFn: () => shopApi.discardLinkInvite(state.customer_id),
    onSuccess: () => {
      toast.success('Pending invite discarded — the link no longer works.')
      queryClient.invalidateQueries({ queryKey: ['shop-link', state.customer_id] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        One-time invite live — the first Telegram account that opens this link claims it. It expires{' '}
        {formatDate(state.claim_expires_at)}.
      </p>
      <div className="flex items-center gap-2">
        <Input readOnly value={state.invite_url ?? ''} onFocus={(e) => e.currentTarget.select()} className="h-8 font-mono text-xs" />
        <Button
          size="sm"
          variant="outline"
          className="h-8 shrink-0 gap-1.5"
          disabled={state.invite_url == null}
          onClick={() => {
            if (state.invite_url) copyInviteLink(state.invite_url)
          }}
        >
          <Link2 className="h-3.5 w-3.5" /> Copy
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-8 shrink-0 text-xs text-destructive hover:text-destructive"
          disabled={discard.isPending}
          onClick={() => {
            if (
              window.confirm(
                `Discard the pending shop-bot invite for ${state.customer_name}?\n\nThe link stops working immediately — nobody has claimed it yet, so nothing else changes.`,
              )
            ) {
              discard.mutate()
            }
          }}
        >
          Discard
        </Button>
      </div>
    </div>
  )
}

// The panel's half of the shop-link flow (models.Customer.shop_user_id on
// the backend): bind a hand-created customer to the shop bot so their
// existing accounts appear in «سرویس‌های من» and a purchase renews their
// real account in place. Minting a link moves no money — this card binds
// identities, never balances.
export function ShopLinkCard({ customerId }: { customerId: number }) {
  const queryClient = useQueryClient()
  const { data: state, isLoading } = useQuery({
    queryKey: ['shop-link', customerId],
    queryFn: () => shopApi.linkState(customerId),
  })

  const invite = useMutation({
    mutationFn: () => shopApi.inviteLink(customerId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['shop-link', customerId] }),
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const unlink = useMutation({
    mutationFn: () => shopApi.unlinkLink(customerId),
    onSuccess: () => {
      toast.success('Shop-bot link removed — the customer loses bot access; their wallet, orders and accounts are untouched.')
      queryClient.invalidateQueries({ queryKey: ['shop-link', customerId] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-1.5 text-[13px] font-semibold">
          <MessageCircle className="h-3.5 w-3.5 text-muted-foreground" /> Shop bot
        </h2>
        {isLoading && <Skeleton className="h-5 w-24" />}
        {state?.status === 'linked' && (
          <Badge>
            <ShieldCheck className="h-3 w-3" /> connected
          </Badge>
        )}
        {state?.status === 'pending' && (
          <Badge variant="secondary">
            <Clock className="h-3 w-3" /> pending invite
          </Badge>
        )}
      </div>

      {!isLoading && state?.status === 'not_linked' && (
        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <p className="text-xs text-muted-foreground">
            Not connected. An invite link lets this customer see their existing accounts in the shop bot, top up a
            wallet, and renew in place — paid from their shop wallet, not this ledger.
          </p>
          <Button size="sm" variant="outline" className="h-8 shrink-0 gap-1.5" disabled={invite.isPending} onClick={() => invite.mutate()}>
            <Link2 className="h-3.5 w-3.5" /> Create invite
          </Button>
        </div>
      )}

      {!isLoading && state?.status === 'pending' && <div className="pt-1">{<PendingBody state={state} />}</div>}

      {!isLoading && state?.status === 'linked' && (
        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <p className="text-xs text-muted-foreground">
            Connected since {formatDate(state.linked_at)} — Telegram id{' '}
            <span className="font-mono tabular-nums">{state.linked_telegram_id}</span>, {state.accounts_linked}{' '}
            {state.accounts_linked === 1 ? 'service' : 'services'} visible in the bot.
          </p>
          <Button
            size="sm"
            variant="ghost"
            className="h-8 shrink-0 text-xs text-destructive hover:text-destructive"
            disabled={unlink.isPending}
            onClick={() => {
              if (
                window.confirm(
                  `Unlink ${state.customer_name} from the shop bot?\n\n` +
                    'They lose bot access immediately. Their shop wallet, past orders and every account are untouched.',
                )
              ) {
                unlink.mutate()
              }
            }}
          >
            Unlink
          </Button>
        </div>
      )}
    </div>
  )
}
