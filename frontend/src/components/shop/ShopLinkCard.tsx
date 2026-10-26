import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { toast } from 'sonner'
import { Clock, Link2, MessageCircle, ShieldCheck, UserPlus } from 'lucide-react'
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
    () => toast.success('لینک دعوت کپی شد — برای مشتری بفرست'),
    () => toast.error('کپی نشد — لینک را دستی انتخاب و کپی کن'),
  )
}

// The operator's pending bind-by-identity claim, rendered on every card
// state: what they typed, and that the customer's FIRST message to the bot
// completes the bind (the bot reports its own numeric id — nobody ever has
// to ask for one). Discardable everywhere.
function BindClaimLine({ state }: { state: ShopLinkState }) {
  const queryClient = useQueryClient()
  const discard = useMutation({
    mutationFn: () => shopApi.discardBindClaim(state.customer_id),
    onSuccess: () => {
      toast.success('ادعای معلق حذف شد')
      queryClient.invalidateQueries({ queryKey: ['shop-link', state.customer_id] })
      queryClient.invalidateQueries({ queryKey: ['connections'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })
  const what = state.bind_claim_username
    ? `@${state.bind_claim_username}`
    : state.bind_claim_display_name || '—'
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-warning/25 bg-warning/10 px-3 py-2">
      <p className="text-xs">
        <span className="inline-flex items-center gap-1 font-medium text-warning">
          <Clock className="h-3 w-3" /> منتظر اولین پیام مشتری در بات
        </span>
        <span className="ml-1.5 text-muted-foreground">
          هر کسی که با <span className="font-medium text-foreground" dir="ltr">{what}</span> وارد بات شود، همان لحظه به این مشتری وصل می‌شود.
        </span>
      </p>
      <Button
        size="sm"
        variant="ghost"
        className="h-7 shrink-0 text-xs text-destructive hover:text-destructive"
        disabled={discard.isPending}
        onClick={() => discard.mutate()}
      >
        انصراف
      </Button>
    </div>
  )
}

function PendingBody({ state }: { state: ShopLinkState }) {
  const queryClient = useQueryClient()
  const discard = useMutation({
    mutationFn: () => shopApi.discardLinkInvite(state.customer_id),
    onSuccess: () => {
      toast.success('دعوت معلق حذف شد — لینک دیگر کار نمی‌کند.')
      queryClient.invalidateQueries({ queryKey: ['shop-link', state.customer_id] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">
        دعوت یک‌بارمصرف فعال است — اولین اکانت تلگرامی که لینک را باز کند آن را تصاحب می‌کند. مهلت:{' '}
        {formatDate(state.claim_expires_at)}.
      </p>
      <div className="flex items-center gap-2">
        <Input readOnly value={state.invite_url ?? ''} onFocus={(e) => e.currentTarget.select()} className="h-8 font-mono text-xs" dir="ltr" />
        <Button
          size="sm"
          variant="outline"
          className="h-8 shrink-0 gap-1.5"
          disabled={state.invite_url == null}
          onClick={() => {
            if (state.invite_url) copyInviteLink(state.invite_url)
          }}
        >
          <Link2 className="h-3.5 w-3.5" /> کپی
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="h-8 shrink-0 text-xs text-destructive hover:text-destructive"
          disabled={discard.isPending}
          onClick={() => {
            if (
              window.confirm(
                `دعوت معلقِ ${state.customer_name} حذف شود؟\n\nلینک بلافاصله از کار می‌افتد — هنوز کسی آن را برنداشته، پس چیز دیگری عوض نمی‌شود.`,
              )
            ) {
              discard.mutate()
            }
          }}
        >
          حذف
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
//
// ONE smart input (owner feedback 2026-10-24 — the numeric id forced an
// awkward "go ask the customer for a number" round trip):
//   pure digits      -> direct bind (the classic operator path)
//   starts with @    -> bind-by-identity claim on the username
//   anything else    -> bind-by-identity claim on the exact display name
// The claim sits pending and the customer's FIRST message to the bot
// completes the bind — the bot reports its own numeric telegram id (see
// backend bot_session's _try_complete_bind_claim). Nothing binds on a guess:
// exactly one pending claim may match, and the same bind guards (group rep,
// payg, already-linked) run at typing time AND at completion time.
function NotLinkedBody({ customerName, customerId }: { customerName: string; customerId: number }) {
  const queryClient = useQueryClient()
  const invite = useMutation({
    mutationFn: () => shopApi.inviteLink(customerId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['shop-link', customerId] }),
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const [identity, setIdentity] = useState('')
  const v = identity.trim()

  // Numeric id = the operator actually knows the number; anything else goes
  // to the pending-claim path. The mode drives both the mutation and the
  // sentence under the input.
  const mode: 'id' | 'username' | 'name' | 'none' =
    /^\d{4,20}$/.test(v) ? 'id' : v.startsWith('@') && v.length >= 4 ? 'username' : v.length >= 2 ? 'name' : 'none'

  const connect = useMutation({
    mutationFn: () => {
      if (mode === 'id') {
        return shopApi.bindTelegram(customerId, { telegram_id: Number(v) }).then(() => 'bind' as const)
      }
      return shopApi
        .createBindClaim(customerId, mode === 'username' ? { telegram_username: v.replace(/^@/, '') } : { display_name: v })
        .then(() => 'claim' as const)
    },
    onSuccess: (kind) => {
      queryClient.invalidateQueries({ queryKey: ['shop-link', customerId] })
      queryClient.invalidateQueries({ queryKey: ['connections'] })
      if (kind === 'bind') {
        toast.success(`وصل شد — سرویس‌های ${customerName} حالا در بات دیده می‌شوند`)
      } else {
        toast.success('ثبت شد — با اولین پیام مشتری در بات، اتصال خودکار انجام می‌شود')
      }
      setIdentity('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const hint =
    mode === 'id'
      ? `آیدی عددی ${v} مستقیم وصل می‌شود — دسترسی واقعی می‌دهد، عدد را چک کن.`
      : mode === 'username'
        ? `هر کسی با @${v.replace(/^@/, '')} وارد بات شود، خودکار به ${customerName} وصل می‌شود.`
        : mode === 'name'
          ? `هر کسی در تلگرام دقیقاً با نام «${v}» وارد بات شود، خودکار به ${customerName} وصل می‌شود. اگر دو نفر هم‌نام‌اند، وصل نمی‌شود — از @username استفاده کن.`
          : 'یکی از این‌ها: @username، یا نام دقیقِ تلگرام، یا آیدی عددی.'

  return (
    <div className="flex flex-col gap-3 pt-1">
      <p className="text-xs text-muted-foreground">
        با وصل‌کردن، مشتری سرویس‌های موجودش را داخل بات می‌بیند و فقط یک عدد دارد: مصرف که کند منفی می‌شود، شارژ که کند اول بدهی پاک می‌شود و بقیه اعتبار می‌ماند.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <Input
          value={identity}
          onChange={(e) => setIdentity(e.currentTarget.value)}
          placeholder="@username یا نام تلگرام یا آیدی عددی…"
          className="h-8 w-64"
          dir="ltr"
          onKeyDown={(e) => {
            if (e.key === 'Enter' && mode !== 'none' && !connect.isPending) connect.mutate()
          }}
        />
        <Button size="sm" className="h-8 gap-1.5" disabled={mode === 'none' || connect.isPending} onClick={() => connect.mutate()}>
          <UserPlus className="h-3.5 w-3.5" />
          {connect.isPending ? 'در حال وصل…' : 'وصل کن'}
        </Button>
        <Button size="sm" variant="outline" className="h-8 gap-1.5" disabled={invite.isPending} onClick={() => invite.mutate()}>
          <Link2 className="h-3.5 w-3.5" /> لینک دعوت
        </Button>
      </div>
      <p className="text-[11px] text-muted-foreground">{hint}</p>
    </div>
  )
}

export function ShopLinkCard({ customerId }: { customerId: number }) {
  const queryClient = useQueryClient()
  const { data: state, isLoading } = useQuery({
    queryKey: ['shop-link', customerId],
    queryFn: () => shopApi.linkState(customerId),
    // While a claim/invite is pending the bind happens in the BOT, not here:
    // poll so the card flips to «وصل است» by itself instead of showing a stale
    // «منتظر» (owner confusion 2026-10-26).
    refetchInterval: (q) => {
      const d = q.state.data
      return d && (d.bind_claim_username || d.bind_claim_display_name || d.status === 'pending') ? 8000 : false
    },
  })

  const unlink = useMutation({
    mutationFn: () => shopApi.unlinkLink(customerId),
    onSuccess: () => {
      toast.success('اتصال برداشته شد — مشتری دیگر به بات دسترسی ندارد؛ کیف پول، سفارش‌ها و اکانت‌ها دست‌نخورده می‌مانند.')
      queryClient.invalidateQueries({ queryKey: ['shop-link', customerId] })
      queryClient.invalidateQueries({ queryKey: ['connections'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const hasPendingClaim = !!(state?.bind_claim_username || state?.bind_claim_display_name)

  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="flex items-center gap-1.5 text-[13px] font-semibold">
          <MessageCircle className="h-3.5 w-3.5 text-muted-foreground" /> بات فروش
        </h2>
        {isLoading && <Skeleton className="h-5 w-24" />}
        {state?.status === 'linked' && (
          <Badge variant="success">
            <ShieldCheck className="h-3 w-3" /> وصل است
          </Badge>
        )}
        {state?.status === 'not_linked' && (
          <Badge variant="outline">به هیچ آیدی وصل نیست</Badge>
        )}
        {state?.status === 'pending' && (
          <Badge variant="secondary">
            <Clock className="h-3 w-3" /> دعوت معلق
          </Badge>
        )}
      </div>

      {!isLoading && hasPendingClaim && state && <div className="pt-1"><BindClaimLine state={state} /></div>}

      {!isLoading && state?.status === 'not_linked' && (
        <NotLinkedBody
          customerName={state.customer_name}
          customerId={state.customer_id}
        />
      )}

      {!isLoading && state?.status === 'pending' && <div className="pt-1"><PendingBody state={state} /></div>}

      {!isLoading && state?.status === 'linked' && (
        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <p className="text-xs text-muted-foreground">
            وصل از {formatDate(state.linked_at)} — آیدی تلگرام{' '}
            <span className="font-mono tabular-nums" dir="ltr">{state.linked_telegram_id}</span>
            {state.linked_display_name || state.linked_telegram_username ? (
              <>
                {' '}(<span className="font-medium text-foreground">{state.linked_display_name ?? '—'}</span>
                {state.linked_telegram_username ? <span dir="ltr"> @{state.linked_telegram_username}</span> : null})
              </>
            ) : null}
            ، {state.accounts_linked} سرویس در بات دیده می‌شود.
            {state.also_linked_customers.length > 0 && (
              <> همین آیدی به مشتری‌های دیگر هم وصل است: <span className="font-medium text-foreground">{state.also_linked_customers.join('، ')}</span>.</>
            )}
          </p>
          <Button
            size="sm"
            variant="ghost"
            className="h-8 shrink-0 text-xs text-destructive hover:text-destructive"
            disabled={unlink.isPending}
            onClick={() => {
              if (
                window.confirm(
                  `اتصال ${state.customer_name} به بات فروش برداشته شود؟\n\nدسترسی بلافاصله قطع می‌شود. کیف پول، سفارش‌های گذشته و همهٔ اکانت‌ها دست‌نخورده می‌مانند.`,
                )
              ) {
                unlink.mutate()
              }
            }}
          >
            قطع اتصال
          </Button>
        </div>
      )}
    </div>
  )
}
