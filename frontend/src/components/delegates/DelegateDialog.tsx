import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { customersApi, groupsApi, delegatesApi, apiErrorMessage } from '@/lib/api'
import type { Delegate, DelegateUpsert } from '@/lib/types'
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
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { SearchableSelect } from '@/components/ui/searchable-select'

type ScopeKind = 'customer' | 'group'

function parseTelegramId(raw: string): number | null {
  // Same strictness as the bot's /delegate_add: digits only (one optional
  // leading '-'), rejecting anything int() would half-accept.
  const body = raw.startsWith('-') ? raw.slice(1) : raw
  if (!/^\d+$/.test(body)) return null
  return Number(raw)
}

function DelegateFields(props: {
  telegramId: string
  onTelegramId: (v: string) => void
  telegramIdDisabled: boolean
  scopeKind: ScopeKind
  onScopeKind: (v: ScopeKind) => void
  scopeKindDisabled: boolean
  scopeId: string
  onScopeId: (v: string) => void
  label: string
  onLabel: (v: string) => void
  creditLimit: string
  onCreditLimit: (v: string) => void
  dailyCap: string
  onDailyCap: (v: string) => void
  prefix: string
  onPrefix: (v: string) => void
  durationDays: string
  onDurationDays: (v: string) => void
}) {
  const p = props
  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list, enabled: p.scopeKind === 'customer' })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list, enabled: p.scopeKind === 'group' })

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="delegate-telegram">Telegram ID</Label>
          <Input
            id="delegate-telegram"
            value={p.telegramId}
            onChange={(e) => p.onTelegramId(e.target.value)}
            placeholder="e.g. 123456789"
            disabled={p.telegramIdDisabled}
            className="font-mono"
          />
          {p.telegramIdDisabled && (
            <p className="text-[11px] text-muted-foreground">
              The grant's identity — to move access to another Telegram account, deactivate this one and create a new
              grant.
            </p>
          )}
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="delegate-label">Label (optional)</Label>
          <Input id="delegate-label" value={p.label} onChange={(e) => p.onLabel(e.target.value)} placeholder="e.g. Reseller — Arman" />
        </div>
      </div>

      <div className="flex flex-col gap-1.5">
        <Label>Scope — whose accounts they can self-manage</Label>
        <div className="grid grid-cols-[auto_1fr] items-center gap-2">
          <Select
            value={p.scopeKind}
            onValueChange={(v) => {
              p.onScopeKind(v as ScopeKind)
              p.onScopeId('')
            }}
            disabled={p.scopeKindDisabled}
          >
            <SelectTrigger className="w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="customer">Customer</SelectItem>
              <SelectItem value="group">Group</SelectItem>
            </SelectContent>
          </Select>
          {p.scopeKind === 'customer' ? (
            <SearchableSelect
              value={p.scopeId}
              onValueChange={p.onScopeId}
              placeholder="Select a customer"
              searchPlaceholder="Search customers…"
              options={(customersQuery.data ?? []).map((c) => ({ value: String(c.id), label: c.name }))}
            />
          ) : (
            <SearchableSelect
              value={p.scopeId}
              onValueChange={p.onScopeId}
              placeholder="Select a group"
              searchPlaceholder="Search groups…"
              options={(groupsQuery.data ?? []).map((g) => ({ value: String(g.id), label: g.name }))}
            />
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          The delegate can create, renew and delete ONLY this {p.scopeKind === 'customer' ? "customer's own" : "group's member"}{' '}
          accounts from their own Telegram — they never see money, the ledger, or any other customer.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="delegate-credit">Credit limit (Toman)</Label>
          <Input
            id="delegate-credit"
            type="number"
            min={0}
            value={p.creditLimit}
            onChange={(e) => p.onCreditLimit(e.target.value)}
            placeholder="empty = no limit"
          />
          <p className="text-[11px] text-muted-foreground">Blocks new create/renew once their posted debt reaches this.</p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="delegate-cap">Daily create cap</Label>
          <Input id="delegate-cap" type="number" min={1} max={500} value={p.dailyCap} onChange={(e) => p.onDailyCap(e.target.value)} />
          <p className="text-[11px] text-muted-foreground">Accounts they can create per rolling 24h (1–500).</p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="delegate-prefix">Username prefix</Label>
          <Input
            id="delegate-prefix"
            value={p.prefix}
            onChange={(e) => p.onPrefix(e.target.value)}
            placeholder="letters/digits/underscore, max 12"
            className="font-mono"
          />
          <p className="text-[11px] text-muted-foreground">Accounts they create are named `{p.prefix || 'prefix'}1`, `{p.prefix || 'prefix'}2`, …</p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="delegate-days">Default duration (days)</Label>
          <Input id="delegate-days" type="number" min={1} max={3650} value={p.durationDays} onChange={(e) => p.onDurationDays(e.target.value)} />
        </div>
      </div>
    </div>
  )
}

export function DelegateDialog({ delegate, trigger }: { delegate?: Delegate; trigger: React.ReactNode }) {
  const editing = delegate != null
  const [open, setOpen] = React.useState(false)
  const [telegramId, setTelegramId] = React.useState('')
  const [scopeKind, setScopeKind] = React.useState<ScopeKind>('customer')
  const [scopeId, setScopeId] = React.useState('')
  const [label, setLabel] = React.useState('')
  const [creditLimit, setCreditLimit] = React.useState('')
  const [dailyCap, setDailyCap] = React.useState('20')
  const [prefix, setPrefix] = React.useState('d')
  const [durationDays, setDurationDays] = React.useState('30')
  const queryClient = useQueryClient()

  React.useEffect(() => {
    if (!open) return
    if (delegate) {
      setTelegramId(String(delegate.telegram_id))
      setScopeKind(delegate.customer_id != null ? 'customer' : 'group')
      setScopeId(String(delegate.customer_id ?? delegate.group_id ?? ''))
      setLabel(delegate.label ?? '')
      setCreditLimit(delegate.credit_limit != null ? String(delegate.credit_limit) : '')
      setDailyCap(String(delegate.daily_create_cap))
      setPrefix(delegate.username_prefix)
      setDurationDays(String(delegate.default_duration_days))
    } else {
      setTelegramId('')
      setScopeKind('customer')
      setScopeId('')
      setLabel('')
      setCreditLimit('')
      setDailyCap('20')
      setPrefix('d')
      setDurationDays('30')
    }
  }, [open, delegate])

  const telegramIdNum = parseTelegramId(telegramId)
  const capNum = Number(dailyCap)
  const daysNum = Number(durationDays)
  const valid =
    telegramIdNum != null &&
    scopeId !== '' &&
    /^\d+$/.test(dailyCap) &&
    capNum >= 1 &&
    capNum <= 500 &&
    /^[a-zA-Z0-9_]+$/.test(prefix) &&
    prefix.length <= 12 &&
    /^\d+$/.test(durationDays) &&
    daysNum >= 1 &&
    daysNum <= 3650 &&
    (creditLimit === '' || (Number.isFinite(Number(creditLimit)) && Number(creditLimit) >= 0))

  const mutation = useMutation({
    mutationFn: () => {
      const credit = creditLimit === '' ? null : Number(creditLimit)
      if (editing && delegate) {
        // Partial edit: send ONLY what changed (plus the telegram_id key) —
        // the backend writes exactly the provided fields and leaves the rest
        // of the stored grant untouched. Re-sending everything would look
        // harmless but resets any field the operator left alone.
        const body: DelegateUpsert = { telegram_id: delegate.telegram_id }
        if (label.trim() !== (delegate.label ?? '')) body.label = label.trim() || null
        if (credit !== delegate.credit_limit) body.credit_limit = credit
        if (capNum !== delegate.daily_create_cap) body.daily_create_cap = capNum
        if (prefix !== delegate.username_prefix) body.username_prefix = prefix
        if (daysNum !== delegate.default_duration_days) body.default_duration_days = daysNum
        const oldScopeId = delegate.customer_id ?? delegate.group_id
        if (scopeKind !== (delegate.customer_id != null ? 'customer' : 'group') || String(oldScopeId) !== scopeId) {
          // Scope changed: send BOTH fields explicitly (one null) so the
          // backend's exactly-one-of rule validates the new pair as a whole
          // instead of merging the new value with the stale one.
          body.customer_id = scopeKind === 'customer' ? Number(scopeId) : null
          body.group_id = scopeKind === 'group' ? Number(scopeId) : null
        }
        return delegatesApi.upsert(body)
      }
      return delegatesApi.upsert({
        telegram_id: telegramIdNum!,
        customer_id: scopeKind === 'customer' ? Number(scopeId) : null,
        group_id: scopeKind === 'group' ? Number(scopeId) : null,
        label: label.trim() || null,
        credit_limit: credit,
        daily_create_cap: capNum,
        username_prefix: prefix,
        default_duration_days: daysNum,
      })
    },
    onSuccess: (d) => {
      toast.success(
        editing
          ? `Delegate ${d.scope_name} updated`
          : `Granted — ${d.scope_name} can now message delegate_bot (Telegram id ${d.telegram_id})`,
      )
      queryClient.invalidateQueries({ queryKey: ['delegates'] })
      setOpen(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{editing ? `Edit delegate — ${delegate!.scope_name}` : 'New delegate grant'}</DialogTitle>
          <DialogDescription>
            {editing
              ? 'Only the fields you actually change are sent — everything else stays exactly as it is.'
              : 'Gives a Telegram account self-service control (create/renew/delete) over one customer or group’s accounts via delegate_bot. Never grants visibility into balances or the ledger.'}
          </DialogDescription>
        </DialogHeader>

        <DelegateFields
          telegramId={telegramId}
          onTelegramId={setTelegramId}
          telegramIdDisabled={editing}
          scopeKind={scopeKind}
          onScopeKind={setScopeKind}
          scopeKindDisabled={false}
          scopeId={scopeId}
          onScopeId={setScopeId}
          label={label}
          onLabel={setLabel}
          creditLimit={creditLimit}
          onCreditLimit={setCreditLimit}
          dailyCap={dailyCap}
          onDailyCap={setDailyCap}
          prefix={prefix}
          onPrefix={setPrefix}
          durationDays={durationDays}
          onDurationDays={setDurationDays}
        />

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button onClick={() => mutation.mutate()} disabled={!valid || mutation.isPending}>
            {mutation.isPending ? 'Saving…' : editing ? 'Save changes' : 'Grant access'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
