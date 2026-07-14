import * as React from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  ArrowDownCircle,
  ArrowUpCircle,
  CalendarClock,
  ChevronDown,
  History,
  Link2,
  Receipt,
  RotateCcw,
  Tag,
  X,
} from 'lucide-react'
import { accountsApi, customersApi, groupsApi, ledgerApi, apiErrorMessage } from '@/lib/api'
import type { AccountRow, AccountRole, BillingMode, LedgerType } from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'
import { UsageBar } from '@/components/UsageBar'
import { Money } from '@/components/Money'
import { StatusDot } from '@/components/StatusDot'
import { cn, daysUntil, formatAgo, formatDate, formatToman } from '@/lib/utils'

const NONE = '__none__'

/** Opens the account inspector on WHATEVER page you're on by setting the
 * `acct` search param — context (filters, scroll, the page itself) survives,
 * which is the whole point vs. the old navigate-away-and-flash pattern. */
export function useOpenAccountInspector() {
  const [searchParams, setSearchParams] = useSearchParams()
  return React.useCallback(
    (accountId: number) => {
      const next = new URLSearchParams(searchParams)
      next.set('acct', String(accountId))
      setSearchParams(next)
    },
    [searchParams, setSearchParams],
  )
}

/** The single place every per-account action lives: adjust time/data, billing
 * rate & mode, ownership, invoicing, usage reset, and the full history — as
 * inline panel sections instead of five separate modals per table row. */
export function AccountInspector() {
  const [searchParams, setSearchParams] = useSearchParams()
  const acctParam = searchParams.get('acct')
  const accountId = acctParam ? Number(acctParam) : null

  const close = React.useCallback(() => {
    const next = new URLSearchParams(searchParams)
    next.delete('acct')
    setSearchParams(next)
  }, [searchParams, setSearchParams])

  React.useEffect(() => {
    if (accountId === null) return
    function onKey(e: KeyboardEvent) {
      // Radix dialogs (palette, modals) preventDefault their own Escape.
      if (e.key === 'Escape' && !e.defaultPrevented) close()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [accountId, close])

  const accountQuery = useQuery({
    queryKey: ['account', accountId],
    queryFn: () => accountsApi.get(accountId!),
    enabled: accountId !== null,
  })

  if (accountId === null) return null

  return (
    <aside
      className="animate-panel-in fixed inset-y-0 right-0 z-40 flex w-full max-w-[400px] flex-col border-l border-border bg-card shadow-2xl"
      aria-label="Account details"
    >
      {accountQuery.isLoading && (
        <div className="flex h-full items-center justify-center text-xs text-muted-foreground">Loading…</div>
      )}
      {accountQuery.isError && (
        <div className="flex h-full flex-col items-center justify-center gap-3 text-xs text-muted-foreground">
          Couldn't load this account.
          <Button size="sm" variant="outline" onClick={close}>Close</Button>
        </div>
      )}
      {accountQuery.data && <InspectorBody key={accountQuery.data.id} account={accountQuery.data} onClose={close} />}
    </aside>
  )
}

function InspectorBody({ account, onClose }: { account: AccountRow; onClose: () => void }) {
  const canBill = !!(account.customer_id || account.group_id)
  const days = daysUntil(account.expire)

  return (
    <>
      <header className="flex items-start justify-between gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <StatusDot status={account.status} />
            <h2 className="truncate font-mono text-sm font-semibold">{account.marzban_username}</h2>
            <Badge variant={account.billing_mode === 'payg' ? 'warning' : 'secondary'}>
              {account.billing_mode === 'payg' ? 'pay-as-you-go' : 'prepay'}
            </Badge>
          </div>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            {account.customer_id ? (
              <Link className="hover:text-foreground hover:underline" to={`/customers/${account.customer_id}`}>
                {account.customer_name ?? `customer #${account.customer_id}`}
              </Link>
            ) : account.group_id ? null : (
              'unassigned'
            )}
            {account.customer_id && account.group_id ? ' · ' : ''}
            {account.group_id && (
              <Link className="hover:text-foreground hover:underline" to={`/groups/${account.group_id}`}>
                {account.group_name ?? `group #${account.group_id}`}
              </Link>
            )}
          </p>
        </div>
        <Button size="icon-sm" variant="ghost" onClick={onClose} aria-label="Close panel">
          <X />
        </Button>
      </header>

      <div className="flex-1 overflow-y-auto">
        {/* Overview — always visible */}
        <div className="flex flex-col gap-3 border-b border-border px-4 py-3">
          <UsageBar used={account.used_traffic} limit={account.data_limit} />
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
            <span className="text-muted-foreground">Expires</span>
            <span
              className={cn(
                'text-right tabular-nums',
                days !== null && days < 0 && 'font-medium text-destructive',
                days !== null && days >= 0 && days <= 3 && 'font-medium text-warning',
              )}
            >
              {account.expire ? `${formatDate(account.expire)} (${days! < 0 ? `${Math.abs(days!)}d ago` : `${days}d`})` : 'never'}
            </span>
            <span className="text-muted-foreground">Monthly average</span>
            <span className="text-right tabular-nums">
              {account.usage_confidence === 'insufficient_data'
                ? 'not enough history'
                : `${account.usage_confidence === 'preliminary' ? '~' : ''}${account.monthly_avg_usage_gb?.toFixed(1)} GB/mo`}
            </span>
            <span className="text-muted-foreground">Rate</span>
            <span className="text-right tabular-nums">
              {!account.rate_configured ? (
                <Badge variant="warning">not set</Badge>
              ) : account.effective_rate > 0 ? (
                `${formatToman(account.effective_rate)}/GB`
              ) : (
                'free'
              )}
            </span>
            <span className="text-muted-foreground">{account.group_id ? "Group's balance" : 'Balance'}</span>
            <span className="text-right">
              <Money amount={account.payer_balance} zero="settled" />
            </span>
            <span className="text-muted-foreground">Last synced</span>
            <span className="text-right text-muted-foreground">{formatAgo(account.last_synced_at)}</span>
          </div>
        </div>

        <AdjustSection account={account} canBill={canBill} />
        <ResetSection account={account} canBill={canBill} />
        <InvoiceSection account={account} canBill={canBill} />
        <BillingSection account={account} />
        <OwnershipSection account={account} />
        <HistorySection account={account} />
      </div>
    </>
  )
}

/* ---------------------------------------------------------------- sections */

function Section({
  icon: Icon,
  title,
  defaultOpen = false,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>
  title: React.ReactNode
  defaultOpen?: boolean
  children: React.ReactNode
}) {
  const [open, setOpen] = React.useState(defaultOpen)
  return (
    <section className="border-b border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-xs font-medium text-foreground hover:bg-muted/50"
      >
        <Icon className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="flex-1">{title}</span>
        <ChevronDown className={cn('h-3.5 w-3.5 text-muted-foreground transition-transform', open && 'rotate-180')} />
      </button>
      {open && <div className="flex flex-col gap-3 px-4 pb-4">{children}</div>}
    </section>
  )
}

function useInvalidateAccount(accountId: number) {
  const queryClient = useQueryClient()
  return React.useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['account'] })
    queryClient.invalidateQueries({ queryKey: ['accounts'] })
    queryClient.invalidateQueries({ queryKey: ['customers'] })
    queryClient.invalidateQueries({ queryKey: ['groups'] })
    queryClient.invalidateQueries({ queryKey: ['ledger'] })
    queryClient.invalidateQueries({ queryKey: ['reports'] })
    queryClient.invalidateQueries({ queryKey: ['account', accountId, 'events'] })
  }, [queryClient, accountId])
}

const DAY_PRESETS = [7, 30, -7]
const GB_PRESETS = [10, 30, 50]

function AdjustSection({ account, canBill }: { account: AccountRow; canBill: boolean }) {
  const [extendDays, setExtendDays] = React.useState('')
  const [extendGb, setExtendGb] = React.useState('')
  const [note, setNote] = React.useState('')
  const [recordCharge, setRecordCharge] = React.useState(true)
  const [chargeAmount, setChargeAmount] = React.useState('')
  const [chargeTouched, setChargeTouched] = React.useState(false)
  const invalidate = useInvalidateAccount(account.id)

  const rate = account.effective_rate > 0 ? account.effective_rate : null
  const suggestedCharge = rate && extendGb && Number(extendGb) > 0 ? Number(extendGb) * rate : 0

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
      invalidate()
      setExtendDays('')
      setExtendGb('')
      setNote('')
      setChargeAmount('')
      setChargeTouched(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const canSubmit = (!!extendDays && Number(extendDays) !== 0) || (!!extendGb && Number(extendGb) !== 0)

  return (
    <Section icon={CalendarClock} title="Adjust time / data" defaultOpen>
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="insp-days" className="text-xs">Days ±</Label>
          <Input id="insp-days" type="number" value={extendDays} onChange={(e) => setExtendDays(e.target.value)} placeholder="30" />
          <div className="flex gap-1">
            {DAY_PRESETS.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setExtendDays(String(d))}
                className="rounded border border-border bg-muted/60 px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                {d > 0 ? `+${d}` : d}
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="insp-gb" className="text-xs">GB ±</Label>
          <Input id="insp-gb" type="number" value={extendGb} onChange={(e) => setExtendGb(e.target.value)} placeholder="10" />
          <div className="flex gap-1">
            {GB_PRESETS.map((g) => (
              <button
                key={g}
                type="button"
                onClick={() => setExtendGb(String(g))}
                className="rounded border border-border bg-muted/60 px-1.5 py-0.5 text-[11px] tabular-nums text-muted-foreground hover:bg-accent hover:text-foreground"
              >
                +{g}
              </button>
            ))}
          </div>
        </div>
      </div>

      {Number(extendGb) > 0 && (
        <div className="rounded-md border border-border bg-muted/40 p-2.5 text-xs">
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={recordCharge}
              disabled={!canBill}
              onChange={(e) => setRecordCharge(e.target.checked)}
            />
            <span className="flex-1">
              {canBill ? (
                <>
                  Also record a debt for this data
                  {rate ? (
                    <span className="text-muted-foreground"> ({extendGb} GB × {formatToman(rate)}/GB)</span>
                  ) : (
                    <span className="text-muted-foreground"> — no rate set, enter an amount</span>
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

      <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note (kept in history) — optional" />
      <Button size="sm" onClick={() => mutation.mutate()} disabled={!canSubmit || mutation.isPending}>
        {mutation.isPending ? 'Applying…' : 'Apply to Marzban'}
      </Button>
    </Section>
  )
}

function ResetSection({ account, canBill }: { account: AccountRow; canBill: boolean }) {
  const [chargeAmount, setChargeAmount] = React.useState('')
  const [note, setNote] = React.useState('')
  const [prefilled, setPrefilled] = React.useState(false)
  const invalidate = useInvalidateAccount(account.id)
  const isPayg = account.billing_mode === 'payg'

  const invoiceQuery = useQuery({
    queryKey: ['account', account.id, 'invoice'],
    queryFn: () => accountsApi.invoice(account.id),
    enabled: isPayg,
  })

  React.useEffect(() => {
    if (invoiceQuery.data && !prefilled) {
      setChargeAmount(invoiceQuery.data.amount > 0 ? String(invoiceQuery.data.amount) : '')
      setPrefilled(true)
    }
  }, [invoiceQuery.data, prefilled])

  const resetMutation = useMutation({
    mutationFn: () =>
      accountsApi.reset(account.id, {
        charge_amount: chargeAmount ? Number(chargeAmount) : undefined,
        note: note || undefined,
      }),
    onSuccess: () => {
      toast.success(`Reset ${account.marzban_username}${chargeAmount ? ` — charged ${formatToman(Number(chargeAmount))}` : ''}`)
      invalidate()
      setNote('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const settleMutation = useMutation({
    mutationFn: () => accountsApi.settle(account.id),
    onSuccess: (r: { charged_amount: number }) => {
      toast.success(`Settled — charged ${formatToman(r.charged_amount)}`)
      invalidate()
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const canSettleStandalone = isPayg && account.group_id === null && canBill

  return (
    <Section icon={RotateCcw} title="Reset usage cycle">
      {isPayg && (
        <div className="rounded-md border border-border bg-muted/40 p-2.5 text-xs">
          {invoiceQuery.isLoading ? (
            <span className="text-muted-foreground">Calculating usage since last settlement…</span>
          ) : invoiceQuery.data ? (
            <div className="flex items-center justify-between gap-2">
              <span className="text-muted-foreground">
                {invoiceQuery.data.billable_gb} GB × {formatToman(invoiceQuery.data.rate_per_gb)}/GB since{' '}
                {invoiceQuery.data.since ? formatDate(invoiceQuery.data.since) : 'the start'}
              </span>
              <span className="font-semibold tabular-nums">{formatToman(invoiceQuery.data.amount)}</span>
            </div>
          ) : null}
        </div>
      )}
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        Sets used traffic back to 0 in Marzban and starts a new billing cycle.
        {isPayg ? ' The suggested charge above is prefilled — edit or clear it before confirming.' : ''}
      </p>
      <div className="grid grid-cols-2 gap-3">
        <Input
          type="number"
          min={0}
          value={chargeAmount}
          onChange={(e) => setChargeAmount(e.target.value)}
          placeholder="Charge (Toman)"
        />
        <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note — optional" />
      </div>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" className="flex-1" onClick={() => resetMutation.mutate()} disabled={resetMutation.isPending}>
          {resetMutation.isPending ? 'Resetting…' : chargeAmount ? `Reset & charge ${formatToman(Number(chargeAmount))}` : 'Reset only'}
        </Button>
        {canSettleStandalone && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => settleMutation.mutate()}
            disabled={settleMutation.isPending || !invoiceQuery.data || invoiceQuery.data.amount <= 0}
            title="Post the accrued usage as a charge and roll the cycle forward, without resetting the quota in Marzban"
          >
            Charge without reset
          </Button>
        )}
      </div>
    </Section>
  )
}

function InvoiceSection({ account, canBill }: { account: AccountRow; canBill: boolean }) {
  const [type, setType] = React.useState<LedgerType>('charge')
  const [volumeGb, setVolumeGb] = React.useState('')
  const [price, setPrice] = React.useState(account.effective_rate ? String(account.effective_rate) : '')
  const [period, setPeriod] = React.useState('')
  const [note, setNote] = React.useState('')
  const invalidate = useInvalidateAccount(account.id)

  const amount = (Number(volumeGb) || 0) * (Number(price) || 0)

  const mutation = useMutation({
    mutationFn: () => {
      // Posts with the account's own customer_id/group_id as-is — never
      // resolving the group's representative as a customer fallback — so the
      // entry shows up in the same balance this account's row displays.
      const parts = [`${volumeGb || 0} GB × ${formatToman(Number(price) || 0)}/GB`]
      if (period) parts.push(`for ${period}`)
      if (note) parts.push(`— ${note}`)
      return ledgerApi.create({
        type,
        amount: Math.round(amount * 100) / 100,
        customer_id: account.customer_id ?? undefined,
        group_id: account.group_id ?? undefined,
        account_id: account.id,
        note: parts.join(' '),
      })
    },
    onSuccess: () => {
      toast.success(`${type === 'charge' ? 'Charged' : 'Credited'} ${formatToman(amount)} for ${account.marzban_username}`)
      invalidate()
      setVolumeGb('')
      setPeriod('')
      setNote('')
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Section icon={Receipt} title="Record an invoice">
      {!canBill && (
        <p className="text-[11px] text-muted-foreground">
          This account isn't assigned to a customer or group yet — assign one below before invoicing it.
        </p>
      )}
      <div className="grid grid-cols-2 gap-1 rounded-md bg-muted p-0.5">
        {(['charge', 'credit'] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setType(t)}
            className={cn(
              'flex items-center justify-center gap-1.5 rounded px-2 py-1 text-xs font-medium transition-colors',
              type === t ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {t === 'charge' ? <ArrowUpCircle className="h-3.5 w-3.5 text-destructive" /> : <ArrowDownCircle className="h-3.5 w-3.5 text-success" />}
            {t === 'charge' ? 'Debt (بدهی)' : 'Credit (طلب)'}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label className="text-xs" htmlFor="insp-inv-gb">Volume (GB)</Label>
          <Input id="insp-inv-gb" type="number" min={0} value={volumeGb} onChange={(e) => setVolumeGb(e.target.value)} placeholder="0" />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label className="text-xs" htmlFor="insp-inv-price">Price (T/GB)</Label>
          <Input id="insp-inv-price" type="number" min={0} value={price} onChange={(e) => setPrice(e.target.value)} placeholder="0" />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Input value={period} onChange={(e) => setPeriod(e.target.value)} placeholder="Period, e.g. Mordad 1405" />
        <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Note — optional" />
      </div>
      <Button size="sm" onClick={() => mutation.mutate()} disabled={mutation.isPending || !canBill || amount <= 0}>
        {mutation.isPending ? 'Saving…' : `${type === 'charge' ? 'Add debt' : 'Add credit'} — ${formatToman(amount)}`}
      </Button>
    </Section>
  )
}

function BillingSection({ account }: { account: AccountRow }) {
  const [rateInput, setRateInput] = React.useState(account.rate_per_gb != null ? String(account.rate_per_gb) : '')
  const [billingMode, setBillingMode] = React.useState<BillingMode>(account.billing_mode)
  const invalidate = useInvalidateAccount(account.id)

  const mutation = useMutation({
    mutationFn: () =>
      accountsApi.updateBilling(account.id, {
        billing_mode: billingMode,
        ...(rateInput ? { rate_per_gb: Number(rateInput) } : { clear_rate: true }),
      }),
    onSuccess: () => {
      toast.success(`Updated billing for ${account.marzban_username}`)
      invalidate()
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Section icon={Tag} title="Billing rate & mode">
      <div className="flex flex-col gap-1.5">
        <Label className="text-xs" htmlFor="insp-rate">Own rate (Toman/GB)</Label>
        <Input id="insp-rate" type="number" value={rateInput} onChange={(e) => setRateInput(e.target.value)} placeholder="blank = inherit" />
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          Blank inherits {account.group_id ? "the group's rate, then " : ''}the dashboard default. A value here overrides
          both — e.g. a per-account discount inside a group.
        </p>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label className="text-xs">Billing mode</Label>
        <Select value={billingMode} onValueChange={(v) => setBillingMode(v as BillingMode)}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="prepay">Prepay — charged manually when a package is sold</SelectItem>
            <SelectItem value="payg">Pay-as-you-go — reset suggests a charge for actual usage</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <Button size="sm" variant="outline" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
        {mutation.isPending ? 'Saving…' : 'Save billing'}
      </Button>
    </Section>
  )
}

function OwnershipSection({ account }: { account: AccountRow }) {
  const [customerId, setCustomerId] = React.useState(account.customer_id ? String(account.customer_id) : NONE)
  const [groupId, setGroupId] = React.useState(account.group_id ? String(account.group_id) : NONE)
  const [role, setRole] = React.useState<AccountRole>(account.role)
  const invalidate = useInvalidateAccount(account.id)

  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })

  const mutation = useMutation({
    mutationFn: () =>
      accountsApi.updateRelationship(account.id, {
        customer_id: customerId === NONE ? null : Number(customerId),
        group_id: groupId === NONE ? null : Number(groupId),
        role,
      }),
    onSuccess: () => {
      toast.success(`Updated ownership for ${account.marzban_username}`)
      invalidate()
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Section icon={Link2} title="Ownership">
      <div className="flex flex-col gap-1.5">
        <Label className="text-xs">Customer</Label>
        <Select value={customerId} onValueChange={setCustomerId}>
          <SelectTrigger>
            <SelectValue placeholder="Unassigned" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NONE}>Unassigned</SelectItem>
            {customersQuery.data?.map((c) => (
              <SelectItem key={c.id} value={String(c.id)}>
                {c.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label className="text-xs">Group (billed together)</Label>
        <Select value={groupId} onValueChange={setGroupId}>
          <SelectTrigger>
            <SelectValue placeholder="None" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NONE}>None</SelectItem>
            {groupsQuery.data?.map((g) => (
              <SelectItem key={g.id} value={String(g.id)}>
                {g.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label className="text-xs">Role</Label>
        <Select value={role} onValueChange={(v) => setRole(v as AccountRole)}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="primary">Primary — the billing contact</SelectItem>
            <SelectItem value="sub">Sub-account — e.g. a family member</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <Button size="sm" variant="outline" onClick={() => mutation.mutate()} disabled={mutation.isPending}>
        {mutation.isPending ? 'Saving…' : 'Save ownership'}
      </Button>
    </Section>
  )
}

function HistorySection({ account }: { account: AccountRow }) {
  const eventsQuery = useQuery({
    queryKey: ['account', account.id, 'events'],
    queryFn: () => accountsApi.events(account.id),
  })
  const ledgerQuery = useQuery({
    queryKey: ['ledger', { accountId: account.id }],
    queryFn: () => ledgerApi.list({ account_id: account.id }),
  })

  type HistoryRow = { key: string; date: string; label: string; detail: string | null; money?: { type: LedgerType; amount: number } }
  const rows: HistoryRow[] = React.useMemo(() => {
    const evts: HistoryRow[] = (eventsQuery.data ?? []).map((e) => ({
      key: `e${e.id}`,
      date: e.date,
      label: e.action.replace(/_/g, ' '),
      detail: e.detail,
    }))
    const money: HistoryRow[] = (ledgerQuery.data ?? []).map((l) => ({
      key: `l${l.id}`,
      date: l.date,
      label: l.type === 'charge' ? 'debt recorded' : 'payment received',
      detail: l.note,
      money: { type: l.type, amount: l.amount },
    }))
    return [...evts, ...money].sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime()).slice(0, 30)
  }, [eventsQuery.data, ledgerQuery.data])

  return (
    <Section icon={History} title="History">
      {rows.length === 0 && <p className="text-xs text-muted-foreground">Nothing recorded yet.</p>}
      <ol className="flex flex-col">
        {rows.map((r) => (
          <li key={r.key} className="flex gap-2.5 border-l border-border py-1.5 pl-3 text-xs [&:first-child]:pt-0">
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-medium capitalize">{r.label}</span>
                <span className="shrink-0 text-[11px] text-muted-foreground">{formatAgo(r.date)}</span>
              </div>
              {r.money ? (
                <span className={cn('tabular-nums', r.money.type === 'charge' ? 'text-destructive' : 'text-success')}>
                  {r.money.type === 'charge' ? '+' : '−'}
                  {formatToman(r.money.amount)}
                </span>
              ) : null}
              {r.detail && <p className="truncate text-muted-foreground" title={r.detail}>{r.detail}</p>}
            </div>
          </li>
        ))}
      </ol>
    </Section>
  )
}
