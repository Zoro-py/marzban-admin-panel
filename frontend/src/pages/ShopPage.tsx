import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { shopApi } from '@/lib/api'
import type { ShopSettings, ShopTopup, ShopUser } from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Checkbox } from '@/components/ui/checkbox'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { EmptyState } from '@/components/EmptyState'
import { AlertTriangle, Check, Store, X } from 'lucide-react'

/** Whole Toman, never decimals — see the shop section header in
 *  backend/app/models.py for why these amounts are integers end to end. */
function toman(amount: number): string {
  return `${Math.round(amount).toLocaleString('en-US')} T`
}

function when(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString()
}

export function ShopPage() {
  const settingsQuery = useQuery({ queryKey: ['shop', 'settings'], queryFn: shopApi.settings })
  const pendingQuery = useQuery({
    queryKey: ['shop', 'topups', 'pending'],
    queryFn: () => shopApi.topups('pending'),
    // Receipts arrive while this page is open and the operator is expected to
    // sit on it — polling is the difference between reviewing a payment in a
    // minute and reviewing it whenever someone remembers to refresh.
    refetchInterval: 30_000,
  })

  const pendingCount = pendingQuery.data?.length ?? 0

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold tracking-tight">Shop</h1>
          <p className="text-xs text-muted-foreground">
            Self-serve sales through the customer bot — wallets, payment receipts, and orders.
          </p>
        </div>
        <ShopStateBadge settings={settingsQuery.data} loading={settingsQuery.isLoading} />
      </div>

      <Tabs defaultValue="topups">
        <TabsList>
          <TabsTrigger value="topups">
            Payments
            {pendingCount > 0 && (
              <span className="ml-1.5 rounded bg-warning/20 px-1.5 text-xs text-warning">{pendingCount}</span>
            )}
          </TabsTrigger>
          <TabsTrigger value="users">Customers</TabsTrigger>
          <TabsTrigger value="orders">Orders</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="topups">
          <PendingTopups query={pendingQuery} />
        </TabsContent>
        <TabsContent value="users">
          <ShopUsers />
        </TabsContent>
        <TabsContent value="orders">
          <ShopOrders />
        </TabsContent>
        <TabsContent value="settings">
          <ShopSettingsForm />
        </TabsContent>
      </Tabs>
    </div>
  )
}

function ShopStateBadge({ settings, loading }: { settings?: ShopSettings; loading: boolean }) {
  if (loading) return <Skeleton className="h-6 w-24" />
  // Not silently blank: whether the shop is open is the first thing the
  // operator looks for on this page.
  if (!settings) {
    return (
      <Badge variant="outline" className="gap-1 text-destructive">
        <Store className="h-3 w-3" /> Status unknown
      </Badge>
    )
  }
  return settings.is_open ? (
    <Badge className="gap-1 bg-success/15 text-success">
      <Store className="h-3 w-3" /> Open · {toman(settings.price_per_gb)}/GB
    </Badge>
  ) : (
    <Badge variant="outline" className="gap-1 text-muted-foreground">
      <Store className="h-3 w-3" /> Closed
    </Badge>
  )
}

function PendingTopups({ query }: { query: ReturnType<typeof useQuery<ShopTopup[]>> }) {
  const queryClient = useQueryClient()
  const [overrides, setOverrides] = React.useState<Record<number, string>>({})
  const [reasons, setReasons] = React.useState<Record<number, string>>({})

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['shop'] })
  }

  const approve = useMutation({
    mutationFn: ({ id, amount }: { id: number; amount?: number }) => shopApi.approveTopup(id, amount),
    onSuccess: (topup) => {
      toast.success(`Credited ${toman(topup.approved_amount ?? topup.claimed_amount)}`)
      invalidate()
    },
  })
  const reject = useMutation({
    mutationFn: ({ id, reason }: { id: number; reason?: string }) => shopApi.rejectTopup(id, reason),
    onSuccess: () => {
      toast.success('Rejected — the customer has been told')
      invalidate()
    },
  })

  if (query.isLoading) return <Skeleton className="h-40 w-full" />
  // An error must never render as "No payments waiting": the operator would
  // read that as nothing to approve while customers who have already paid
  // sit in the queue.
  if (query.isError) return <LoadFailed what="pending payments" onRetry={() => query.refetch()} />
  if (!query.data?.length) {
    return (
      <EmptyState
        title="No payments waiting"
        description="Receipts uploaded in the shop bot land here, and are also pushed to your Telegram with approve/reject buttons."
      />
    )
  }

  return (
    <div className="flex flex-col gap-2">
      {/* Said once, at the top, rather than per row: the receipt IMAGE only
          exists inside Telegram (the backend stores a file_id, never the
          bytes — customer bank receipts are not something to keep on disk).
          Without this note the operator would look for a thumbnail that is
          never going to appear here. */}
      <p className="text-xs text-muted-foreground">
        The receipt image is in your Telegram — the backend deliberately stores only a reference to it, not
        the file. Approve from there, or here once you've looked at it.
      </p>
      {query.data.map((topup) => {
        // Every approve/reject button waits while ANY decision is in flight.
        // approve and reject are single mutations shared by all rows, so a
        // per-row check (variables.id === topup.id) broke as soon as a second
        // row was clicked: the new call replaced `variables`, row A's button
        // re-enabled mid-request, and a second click earned the operator an
        // "already approved" error on a payment they had approved once. The
        // backend still credits only once — this is about not lying on screen.
        const busy = approve.isPending || reject.isPending
        const overrideRaw = overrides[topup.id] ?? ''
        const overrideAmount = overrideRaw.trim() === '' ? undefined : Number(overrideRaw)
        // Whole Toman only — the backend field is an int (wallet amounts are
        // integers end to end), so a decimal would come back as a 422 after
        // the click instead of being caught here.
        const overrideInvalid =
          overrideAmount !== undefined && (!Number.isInteger(overrideAmount) || overrideAmount <= 0)

        return (
          <Card key={topup.id}>
            <CardContent className="flex flex-col gap-3 pt-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <div className="text-sm font-medium">
                    {topup.display_name ?? 'Unknown'}{' '}
                    <span className="text-xs text-muted-foreground">#{topup.id} · id {topup.telegram_id}</span>
                    {/* The code the customer holds. When they write "what happened
                        to A7K2?", this is what you scan for. */}
                    {topup.reference_code && (
                      <Badge variant="outline" className="ml-1.5 font-mono text-xs">
                        {topup.reference_code}
                      </Badge>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground">{when(topup.created_at)}</div>
                  {/* Approving an order-bound payment also DELIVERS the plan, and
                      approving less than it costs leaves the customer waiting —
                      so the two kinds must not look the same. */}
                  <div className="text-xs">
                    {topup.order_id ? (
                      <span className="text-primary">For order #{topup.order_id} — approving delivers it</span>
                    ) : (
                      <span className="text-muted-foreground">Wallet credit only</span>
                    )}
                  </div>
                </div>
                <div className="text-sm font-medium tabular-nums">{toman(topup.claimed_amount)} claimed</div>
              </div>

              <div className="flex flex-wrap items-end gap-2">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`amt-${topup.id}`} className="text-xs">
                    Credit a different amount (optional)
                  </Label>
                  <Input
                    id={`amt-${topup.id}`}
                    type="number"
                    className="h-8 w-40"
                    placeholder={`${topup.claimed_amount} Toman`}
                    value={overrideRaw}
                    onChange={(e) => setOverrides((o) => ({ ...o, [topup.id]: e.target.value }))}
                  />
                </div>
                <div className="flex flex-1 flex-col gap-1.5">
                  <Label htmlFor={`why-${topup.id}`} className="text-xs">
                    Rejection reason (shown to the customer)
                  </Label>
                  <Input
                    id={`why-${topup.id}`}
                    className="h-8"
                    placeholder="e.g. receipt unreadable"
                    value={reasons[topup.id] ?? ''}
                    onChange={(e) => setReasons((r) => ({ ...r, [topup.id]: e.target.value }))}
                  />
                </div>
              </div>

              {overrideInvalid && (
                <p className="flex items-center gap-1.5 text-xs text-destructive">
                  <AlertTriangle className="h-3.5 w-3.5" /> Enter a positive whole amount in Toman, or leave it
                  blank to credit what they claimed.
                </p>
              )}

              <div className="flex gap-2">
                <Button
                  size="sm"
                  className="gap-1.5"
                  disabled={busy || overrideInvalid}
                  onClick={() => approve.mutate({ id: topup.id, amount: overrideAmount })}
                >
                  <Check className="h-3.5 w-3.5" />
                  Credit {toman(overrideAmount ?? topup.claimed_amount)}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1.5"
                  disabled={busy}
                  onClick={() => {
                    // The customer is told immediately and cannot appeal in the
                    // bot, so a mis-tap here is the one decision they feel as
                    // "they took my money and said no".
                    const who = topup.display_name ?? `id ${topup.telegram_id}`
                    if (!window.confirm(`Reject ${toman(topup.claimed_amount)} from ${who}? They are told straight away.`)) return
                    reject.mutate({ id: topup.id, reason: reasons[topup.id] || undefined })
                  }}
                >
                  <X className="h-3.5 w-3.5" /> Reject
                </Button>
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}

function ShopUsers() {
  const queryClient = useQueryClient()
  const usersQuery = useQuery({ queryKey: ['shop', 'users'], queryFn: shopApi.users })
  const [adjusting, setAdjusting] = React.useState<Record<number, string>>({})

  const block = useMutation({
    mutationFn: ({ id, blocked }: { id: number; blocked: boolean }) =>
      shopApi.updateUser(id, { is_blocked: blocked }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['shop'] }),
  })
  const adjust = useMutation({
    mutationFn: ({ id, amount }: { id: number; amount: number }) =>
      shopApi.adjustWallet(id, { amount, note: 'Manual adjustment from dashboard' }),
    onSuccess: (user) => {
      toast.success(`New balance: ${toman(user.balance)}`)
      setAdjusting((a) => ({ ...a, [user.id]: '' }))
      queryClient.invalidateQueries({ queryKey: ['shop'] })
    },
  })

  if (usersQuery.isLoading) return <Skeleton className="h-40 w-full" />
  if (usersQuery.isError) return <LoadFailed what="shop customers" onRetry={() => usersQuery.refetch()} />
  if (!usersQuery.data?.length) {
    return <EmptyState title="No shop customers yet" description="Anyone who opens the shop bot appears here." />
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Customer</TableHead>
          <TableHead className="text-right">Balance</TableHead>
          <TableHead>Last seen</TableHead>
          <TableHead className="w-64">Adjust wallet</TableHead>
          <TableHead className="w-24">Blocked</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {usersQuery.data.map((user: ShopUser) => {
          const raw = adjusting[user.id] ?? ''
          const amount = raw.trim() === '' ? null : Number(raw)
          // Whole Toman, never zero: wallet amounts are integers end to end.
          const valid = amount !== null && Number.isInteger(amount) && amount !== 0
          return (
            <TableRow key={user.id}>
              <TableCell>
                <div className="text-xs font-medium">{user.display_name ?? '—'}</div>
                <div className="text-xs text-muted-foreground">
                  {user.telegram_username ? `@${user.telegram_username} · ` : ''}id {user.telegram_id}
                </div>
              </TableCell>
              <TableCell className="text-right tabular-nums">
                <span className={user.balance > 0 ? 'text-success' : 'text-muted-foreground'}>
                  {toman(user.balance)}
                </span>
              </TableCell>
              <TableCell className="text-xs text-muted-foreground">{when(user.last_seen_at)}</TableCell>
              <TableCell>
                <div className="flex gap-1.5">
                  <Input
                    className="h-8"
                    type="number"
                    placeholder="+/- Toman"
                    value={raw}
                    onChange={(e) => setAdjusting((a) => ({ ...a, [user.id]: e.target.value }))}
                  />
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!valid || adjust.isPending}
                    onClick={() => {
                      if (!valid) return
                      // Moves a customer's money in one click, so it says the
                      // result out loud first — a mistyped minus sign is
                      // otherwise discovered by the customer, not the operator.
                      const next = user.balance + amount!
                      const verb = amount! > 0 ? 'Add' : 'Remove'
                      if (!window.confirm(
                        `${verb} ${toman(Math.abs(amount!))} ${amount! > 0 ? 'to' : 'from'} ` +
                        `${user.display_name ?? user.telegram_id}'s wallet?
` +
                        `Balance: ${toman(user.balance)} → ${toman(next)}`,
                      )) return
                      adjust.mutate({ id: user.id, amount: amount! })
                    }}
                  >
                    Apply
                  </Button>
                </div>
              </TableCell>
              <TableCell>
                <Checkbox
                  checked={user.is_blocked}
                  onCheckedChange={(checked) => {
                    // One stray tap in the wrong row otherwise cuts a paying
                    // customer off the shop with no undo and no notice.
                    const who = user.display_name ?? String(user.telegram_id)
                    const verb = checked ? 'Block' : 'Unblock'
                    if (!window.confirm(`${verb} ${who}?`)) return
                    block.mutate({ id: user.id, blocked: Boolean(checked) })
                  }}
                  aria-label={`Block ${user.display_name ?? user.telegram_id}`}
                />
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

function ShopOrders() {
  const ordersQuery = useQuery({ queryKey: ['shop', 'orders'], queryFn: shopApi.orders })
  if (ordersQuery.isLoading) return <Skeleton className="h-40 w-full" />
  if (ordersQuery.isError) return <LoadFailed what="orders" onRetry={() => ordersQuery.refetch()} />
  if (!ordersQuery.data?.length) {
    return <EmptyState title="No orders yet" description="Self-serve purchases appear here as they happen." />
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>When</TableHead>
          <TableHead>Customer</TableHead>
          <TableHead>Account</TableHead>
          <TableHead className="text-right">Plan</TableHead>
          <TableHead className="text-right">Paid</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {ordersQuery.data.map((order) => (
          <TableRow key={order.id}>
            <TableCell className="text-xs text-muted-foreground">{when(order.created_at)}</TableCell>
            <TableCell className="text-xs">{order.display_name ?? order.telegram_id ?? '—'}</TableCell>
            <TableCell className="text-xs font-medium">{order.marzban_username ?? '—'}</TableCell>
            <TableCell className="text-right text-xs tabular-nums">
              {order.data_limit_gb}GB · {order.duration_days}d
            </TableCell>
            {/* "Paid" only when money actually changed hands for this order:
                an awaiting order has paid nothing, a failed one was refunded,
                and a trial was free by design. */}
            <TableCell className="text-right text-xs tabular-nums">
              {order.price === 0 ? (
                <span className="text-muted-foreground">
                  {order.is_provisional ? 'bridge' : 'trial'}
                </span>
              ) : order.status === 'delivered' || order.status === 'provisioning' ? (
                toman(order.price)
              ) : (
                <span className="text-muted-foreground" title={`Price ${toman(order.price)} — not paid`}>—</span>
              )}
            </TableCell>
            <TableCell>
              {/* Chosen but not paid: holds no money and needs nothing from
                  you until a receipt for it arrives. Muted on purpose — it is
                  a customer's intention, not a problem. */}
              {order.status === 'awaiting_payment' && (
                <Badge variant="outline" className="text-muted-foreground">awaiting payment</Badge>
              )}
              {order.status === 'delivered' && <Badge className="bg-success/15 text-success">delivered</Badge>}
              {order.status === 'provisioning' && <Badge variant="outline">provisioning</Badge>}
              {order.status === 'failed' && (
                <Badge className="bg-destructive/15 text-destructive" title={order.error ?? undefined}>
                  failed · refunded
                </Badge>
              )}
              {order.error && order.status === 'delivered' && (
                <span className="ml-1 text-xs text-warning" title={order.error}>
                  ⚠
                </span>
              )}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function ShopSettingsForm() {
  const queryClient = useQueryClient()
  const settingsQuery = useQuery({ queryKey: ['shop', 'settings'], queryFn: shopApi.settings })
  const [draft, setDraft] = React.useState<Partial<ShopSettings>>({})

  // Seeded from the server exactly once per load, then owned locally — so a
  // background refetch can't overwrite half-typed input mid-edit. The effect
  // alone did not do that: approving a payment invalidates the whole ['shop']
  // key, the settings refetch re-ran this, and a half-typed card number was
  // replaced by the server's copy with no warning.
  const seeded = React.useRef(false)
  React.useEffect(() => {
    if (settingsQuery.data && !seeded.current) {
      setDraft(settingsQuery.data)
      seeded.current = true
    }
  }, [settingsQuery.data])

  // An emptied number input reads back as 0, and saving that would set the
  // price of a plan to nothing — the shop would hand out service for free
  // until someone noticed. Blocked at the button rather than relying on the
  // backend's 422 after the fact.
  const positive = (v: unknown) => typeof v === 'number' && Number.isFinite(v) && v > 0
  const nonNegative = (v: unknown) => typeof v === 'number' && Number.isFinite(v) && v >= 0
  const settingsInvalid =
    ![draft.price_per_gb, draft.plan_duration_days, draft.min_gb, draft.max_gb, draft.max_topup].every(positive) ||
    // A minimum top-up of zero is a real choice: it means "no minimum".
    !nonNegative(draft.min_topup)

  const save = useMutation({
    mutationFn: () =>
      shopApi.updateSettings({
        is_open: draft.is_open,
        price_per_gb: Number(draft.price_per_gb ?? 0),
        min_gb: Number(draft.min_gb ?? 0),
        max_gb: Number(draft.max_gb ?? 0),
        plan_duration_days: Number(draft.plan_duration_days ?? 30),
        card_number: draft.card_number ?? null,
        card_holder: draft.card_holder ?? null,
        username_prefix: draft.username_prefix ?? 'shop',
        min_topup: Number(draft.min_topup ?? 0),
        max_topup: Number(draft.max_topup ?? 0),
        shop_name: draft.shop_name?.trim() || null,
        support_handle: draft.support_handle?.trim() || null,
        approval_eta_minutes: Number(draft.approval_eta_minutes ?? 30),
        provisional_enabled: Boolean(draft.provisional_enabled),
        provisional_gb: Number(draft.provisional_gb ?? 1),
        provisional_hours: Number(draft.provisional_hours ?? 24),
        trial_enabled: Boolean(draft.trial_enabled),
        trial_gb: Number(draft.trial_gb ?? 1),
        trial_hours: Number(draft.trial_hours ?? 24),
      }),
    onSuccess: () => {
      toast.success('Shop settings saved')
      queryClient.invalidateQueries({ queryKey: ['shop'] })
    },
  })

  if (settingsQuery.isLoading) return <Skeleton className="h-64 w-full" />
  // Never render the form without the real settings behind it. The draft is
  // seeded from the server; if that load failed the draft is empty, and Save
  // would send price 0 and card null — wiping the live shop's price and card
  // number with one click on a form that merely failed to load.
  if (settingsQuery.isError || !settingsQuery.data) {
    return <LoadFailed what="shop settings" onRetry={() => settingsQuery.refetch()} />
  }

  const set = <K extends keyof ShopSettings>(key: K, value: ShopSettings[K]) =>
    setDraft((d) => ({ ...d, [key]: value }))

  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle>Shop settings</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <label className="flex items-center gap-2">
          <Checkbox checked={Boolean(draft.is_open)} onCheckedChange={(c) => set('is_open', Boolean(c))} />
          <span className="text-sm">Shop is open</span>
        </label>
        {/* Stated up front, because the backend refuses the combination and a
            400 after clicking Save is a worse way to learn it. */}
        <p className="text-xs text-muted-foreground">
          The shop can't be opened without a price per GB and a card number — customers would see a buy
          button that can't complete, and nowhere to send money.
        </p>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Price per GB (Toman)">
            <Input
              type="number"
              value={draft.price_per_gb ?? ''}
              onChange={(e) => set('price_per_gb', Number(e.target.value))}
            />
          </Field>
          <Field label="Plan length (days)">
            <Input
              type="number"
              value={draft.plan_duration_days ?? ''}
              onChange={(e) => set('plan_duration_days', Number(e.target.value))}
            />
          </Field>
          <Field label="Smallest plan (GB)">
            <Input type="number" value={draft.min_gb ?? ''} onChange={(e) => set('min_gb', Number(e.target.value))} />
          </Field>
          <Field label="Largest plan (GB)">
            <Input type="number" value={draft.max_gb ?? ''} onChange={(e) => set('max_gb', Number(e.target.value))} />
          </Field>
          <Field label="Card number">
            <Input
              value={draft.card_number ?? ''}
              onChange={(e) => set('card_number', e.target.value)}
              placeholder="6037 xxxx xxxx xxxx"
            />
          </Field>
          <Field label="Card holder name">
            <Input value={draft.card_holder ?? ''} onChange={(e) => set('card_holder', e.target.value)} />
          </Field>
          <Field label="Smallest top-up (Toman)">
            <Input
              type="number"
              value={draft.min_topup ?? ''}
              onChange={(e) => set('min_topup', Number(e.target.value))}
            />
          </Field>
          <Field label="Largest top-up (Toman)">
            <Input
              type="number"
              value={draft.max_topup ?? ''}
              onChange={(e) => set('max_topup', Number(e.target.value))}
            />
          </Field>
          <Field label="Username prefix for sold accounts">
            <Input
              value={draft.username_prefix ?? ''}
              onChange={(e) => set('username_prefix', e.target.value)}
              placeholder="shop"
            />
          </Field>
        </div>

        {/* Who the customer is buying from. A nameless bot with no reachable
            person is indistinguishable from every other shop asking for a card
            transfer — these two fields are most of what makes it trustworthy. */}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Shop name (shown to customers)">
            <Input
              value={draft.shop_name ?? ''}
              onChange={(e) => set('shop_name', e.target.value)}
              placeholder="e.g. Nova VPN"
            />
          </Field>
          <Field label="Support Telegram handle">
            <Input
              value={draft.support_handle ?? ''}
              onChange={(e) => set('support_handle', e.target.value)}
              placeholder="@yourname"
            />
          </Field>
          <Field label="Promised approval time (minutes)">
            <Input
              type="number"
              value={draft.approval_eta_minutes ?? ''}
              onChange={(e) => set('approval_eta_minutes', Number(e.target.value))}
            />
          </Field>
        </div>
        <p className="text-xs text-muted-foreground">
          Customers are told this number the moment they send a receipt. Set what you can keep on a bad day, not a
          good one — it is a promise, and a missed one costs more trust than a longer honest one.
        </p>

        <div className="flex flex-col gap-3 rounded-md border p-3">
          <label className="flex items-center gap-2">
            <Checkbox
              checked={Boolean(draft.provisional_enabled)}
              onCheckedChange={(c) => set('provisional_enabled', Boolean(c))}
            />
            <span className="text-sm">Hand over a small service as soon as a receipt arrives</span>
          </label>
          <p className="text-xs text-muted-foreground">
            The wait for your approval is where a first-time buyer decides they have been robbed. This connects them in
            seconds on the link their real plan extends in place once you approve. Only for a receipt claiming most of
            the plan's price, once per customer per 30 days, never after a payment of theirs was rejected, and it stops
            the moment you reject one.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Bridge volume (GB)">
              <Input
                type="number"
                value={draft.provisional_gb ?? ''}
                onChange={(e) => set('provisional_gb', Number(e.target.value))}
                disabled={!draft.provisional_enabled}
              />
            </Field>
            <Field label="Bridge length (hours)">
              <Input
                type="number"
                value={draft.provisional_hours ?? ''}
                onChange={(e) => set('provisional_hours', Number(e.target.value))}
                disabled={!draft.provisional_enabled}
              />
            </Field>
          </div>
        </div>

        <div className="flex flex-col gap-3 rounded-md border p-3">
          <label className="flex items-center gap-2">
            <Checkbox
              checked={Boolean(draft.trial_enabled)}
              onCheckedChange={(c) => set('trial_enabled', Boolean(c))}
            />
            <span className="text-sm">Offer a free trial to new customers</span>
          </label>
          <p className="text-xs text-muted-foreground">
            One per Telegram account, created instantly with no payment. It lets a stranger see the service work before
            sending money — the one thing no card-to-card shop can otherwise offer.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Trial volume (GB)">
              <Input
                type="number"
                value={draft.trial_gb ?? ''}
                onChange={(e) => set('trial_gb', Number(e.target.value))}
                disabled={!draft.trial_enabled}
              />
            </Field>
            <Field label="Trial length (hours)">
              <Input
                type="number"
                value={draft.trial_hours ?? ''}
                onChange={(e) => set('trial_hours', Number(e.target.value))}
                disabled={!draft.trial_enabled}
              />
            </Field>
          </div>
        </div>

        <div>
          {settingsInvalid && (
            <p className="text-xs text-destructive">
              Price, duration, volume range and top-up range must all be greater than zero.
            </p>
          )}
          <Button onClick={() => save.mutate()} disabled={save.isPending || settingsInvalid}>
            {save.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function LoadFailed({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <div className="flex items-center gap-3 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-3 text-sm">
      <AlertTriangle className="h-4 w-4 shrink-0 text-destructive" />
      <span className="flex-1">Couldn't load {what}. This is a loading error — not an empty list.</span>
      <Button size="sm" variant="outline" onClick={onRetry}>
        Retry
      </Button>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label className="text-xs">{label}</Label>
      {children}
    </div>
  )
}
