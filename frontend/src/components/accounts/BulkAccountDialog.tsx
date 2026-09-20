import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, apiErrorMessage, customersApi, groupsApi } from '@/lib/api'
import type { BulkAccountRequest, BulkAccountResult } from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
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
import { SearchableSelect } from '@/components/ui/searchable-select'
import { AlertTriangle, Check, Copy, Users } from 'lucide-react'

const NONE = '__none__'
// Deliberate opt-out of the default family customer (test / one-off accounts).
const UNASSIGNED = '__unassigned__'
// Mirrors MAX_BULK_COUNT in backend/app/bulk_accounts.py. Kept as a local
// constant rather than fetched: the input needs a max before any request is
// made, and the backend rejects an over-cap count regardless — this only
// decides whether the operator finds out before or after clicking.
const MAX_COUNT = 50
// Long enough that a preview isn't refetched on every keystroke. Each preview
// makes the backend page through the panel's ENTIRE user list, so this is a
// real cost on a big panel, not just a rendering nicety.
const PREVIEW_DEBOUNCE_MS = 600

function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = React.useState(value)
  React.useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])
  return debounced
}

interface BulkAccountDialogProps {
  defaultCustomerId?: number
  defaultGroupId?: number
  trigger?: React.ReactNode
}

function isValidBatchNames(base: string, count: number, start: number | null): boolean {
  return (
    /^[a-zA-Z0-9_]+$/.test(base) &&
    base.length >= 2 &&
    base.length <= 28 &&
    Number.isInteger(count) &&
    count >= 1 &&
    count <= MAX_COUNT &&
    (start === null || (Number.isInteger(start) && start >= 1))
  )
}

export function BulkAccountDialog({ defaultCustomerId, defaultGroupId, trigger }: BulkAccountDialogProps) {
  const [open, setOpen] = React.useState(false)
  const [baseName, setBaseName] = React.useState('')
  const [count, setCount] = React.useState('5')
  const [startIndex, setStartIndex] = React.useState('')
  const [customerId, setCustomerId] = React.useState<string>(defaultCustomerId ? String(defaultCustomerId) : NONE)
  const [groupId, setGroupId] = React.useState<string>(defaultGroupId ? String(defaultGroupId) : NONE)
  const [expireDays, setExpireDays] = React.useState('30')
  const [dataLimitGb, setDataLimitGb] = React.useState('')
  const [ratePerGb, setRatePerGb] = React.useState('')
  const [autoRenewEnabled, setAutoRenewEnabled] = React.useState(true)
  const [result, setResult] = React.useState<BulkAccountResult | null>(null)
  const queryClient = useQueryClient()

  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list, enabled: open })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list, enabled: open })

  const trimmedBase = baseName.trim()
  const parsedCount = Number(count)
  const parsedStart = startIndex.trim() === '' ? null : Number(startIndex)

  // Only the fields that change WHICH usernames get made are debounced into
  // the preview — plan size and ownership don't affect the names, so typing a
  // data limit shouldn't trigger another full panel scan.
  const previewInput = useDebounced(
    JSON.stringify({ base: trimmedBase, count: parsedCount, start: parsedStart }),
    PREVIEW_DEBOUNCE_MS,
  )

  // Choosing a group makes «No owner» meaningless and removes it from the list;
  // don't leave the select holding a value that is no longer an option.
  React.useEffect(() => {
    if (groupId !== NONE && customerId === UNASSIGNED) setCustomerId(NONE)
  }, [groupId, customerId])

  const nameInputValid = isValidBatchNames(trimmedBase, parsedCount, parsedStart)
  // The preview is keyed on the DEBOUNCED input, so it must be enabled by the
  // validity of that same debounced input — not of what is in the box right
  // now. Enabling it from the live value fired a request with the stale
  // (still empty) base name the moment the second character was typed, and the
  // resulting 422 unmounted the whole dialog.
  const debouncedInput = JSON.parse(previewInput) as { base: string; count: number; start: number | null }
  const previewInputValid = isValidBatchNames(debouncedInput.base, debouncedInput.count, debouncedInput.start)

  const previewQuery = useQuery({
    queryKey: ['accounts', 'bulk-preview', previewInput],
    queryFn: () => {
      const { base, count: c, start } = JSON.parse(previewInput)
      return accountsApi.previewBulk({ base_name: base, count: c, start_index: start })
    },
    enabled: open && previewInputValid && !result,
    // The panel's user list barely moves between two previews seconds apart,
    // and refetching it on every dialog focus would be a full scan each time.
    staleTime: 30_000,
    retry: false,
    // This query shows its own error inline, right under the inputs it
    // belongs to — see main.tsx for why that suppresses the global toast.
    meta: { silentError: true },
  })

  const buildBody = (): BulkAccountRequest => ({
    base_name: trimmedBase,
    count: parsedCount,
    start_index: parsedStart,
    customer_id: customerId === NONE || customerId === UNASSIGNED ? null : Number(customerId),
    // "No owner" only means something without a group; with one, the group owns them.
    unassigned: customerId === UNASSIGNED && groupId === NONE,
    group_id: groupId === NONE ? null : Number(groupId),
    expire_days: expireDays.trim() === '' ? null : Number(expireDays),
    data_limit_gb: dataLimitGb.trim() === '' ? null : Number(dataLimitGb),
    rate_per_gb: ratePerGb.trim() === '' ? null : Number(ratePerGb),
    auto_renew_enabled: autoRenewEnabled,
  })

  const mutation = useMutation({
    mutationFn: () => accountsApi.createBulk(buildBody()),
    onSuccess: (data) => {
      setResult(data)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      if (data.failed > 0) {
        toast.warning(`${data.created} created, ${data.failed} failed — see the list`)
      } else {
        toast.success(`Created ${data.created} account${data.created === 1 ? '' : 's'}`)
      }
    },
    // No local onError: main.tsx's MutationCache already toasts every mutation
    // failure. Adding one here would be the duplicate-toast pattern the rest
    // of this app has, not a fix for it.
  })

  const resetForm = () => {
    setBaseName('')
    setStartIndex('')
    setDataLimitGb('')
    setRatePerGb('')
    setResult(null)
  }

  const handleOpenChange = (next: boolean) => {
    // Never close mid-flight: the request is still creating real accounts in
    // Marzban, and dropping the response would leave the operator with no
    // record of which ones made it.
    if (!next && mutation.isPending) return
    setOpen(next)
    if (!next) resetForm()
  }

  const copy = (text: string) => {
    navigator.clipboard?.writeText(text).then(
      () => toast.success('Subscription link copied'),
      () => toast.error('Could not copy — select the link and copy it manually'),
    )
  }

  const previewNames = previewQuery.data?.names ?? []
  const willCreate = previewQuery.data?.will_create ?? 0
  const willSkip = previewQuery.data?.will_skip ?? 0

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        {trigger ?? (
          <Button size="sm" variant="outline" className="gap-1.5">
            <Users className="h-4 w-4" /> Family batch
          </Button>
        )}
      </DialogTrigger>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{result ? 'Batch finished' : 'Create a family batch'}</DialogTitle>
          <DialogDescription>
            {result
              ? 'Each created account is listed below with its subscription link.'
              : 'One name, many accounts: “khanevade” becomes khanevade1, khanevade2, … Each one gets a Telegram message with its QR code and link.'}
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <BatchResult result={result} onCopy={copy} />
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (!nameInputValid || willCreate === 0) return
              mutation.mutate()
            }}
          >
            <div className="flex flex-col gap-3">
              <div className="grid grid-cols-[2fr_1fr_1fr] gap-3">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="bulk-base">Base name</Label>
                  <Input
                    id="bulk-base"
                    value={baseName}
                    onChange={(e) => setBaseName(e.target.value)}
                    placeholder="khanevade"
                    autoFocus
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="bulk-count">How many</Label>
                  <Input
                    id="bulk-count"
                    type="number"
                    min={1}
                    max={MAX_COUNT}
                    value={count}
                    onChange={(e) => setCount(e.target.value)}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="bulk-start">Start at</Label>
                  <Input
                    id="bulk-start"
                    type="number"
                    min={1}
                    value={startIndex}
                    onChange={(e) => setStartIndex(e.target.value)}
                    placeholder="auto"
                  />
                </div>
              </div>

              <NamePreview
                enabled={nameInputValid}
                loading={previewQuery.isFetching}
                error={previewQuery.error ? apiErrorMessage(previewQuery.error) : null}
                names={previewNames}
                willCreate={willCreate}
                willSkip={willSkip}
              />

              <div className="grid grid-cols-2 gap-3">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="bulk-days">Expires in (days, blank = never)</Label>
                  <Input
                    id="bulk-days"
                    type="number"
                    value={expireDays}
                    onChange={(e) => setExpireDays(e.target.value)}
                    placeholder="30"
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="bulk-gb">Data limit each (GB, blank = unlimited)</Label>
                  <Input
                    id="bulk-gb"
                    type="number"
                    value={dataLimitGb}
                    onChange={(e) => setDataLimitGb(e.target.value)}
                    placeholder="30"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="flex flex-col gap-1.5">
                  <Label>Customer</Label>
                  <SearchableSelect
                    value={customerId}
                    onValueChange={setCustomerId}
                    placeholder="New family customer"
                    searchPlaceholder="Search customers…"
                    options={[
                      {
                        value: NONE,
                        label: groupId === NONE
                          ? `New family customer${trimmedBase ? ` «${trimmedBase}»` : ''} (default)`
                          : 'None (the group owns them)',
                      },
                      ...(groupId === NONE ? [{ value: UNASSIGNED, label: 'No owner (test accounts)' }] : []),
                      ...(customersQuery.data ?? []).map((c) => ({ value: String(c.id), label: c.name })),
                    ]}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label>Group (pay-as-you-go)</Label>
                  <SearchableSelect
                    value={groupId}
                    onValueChange={setGroupId}
                    placeholder="None"
                    searchPlaceholder="Search groups…"
                    options={[
                      { value: NONE, label: 'None' },
                      ...(groupsQuery.data ?? []).map((g) => ({ value: String(g.id), label: g.name })),
                    ]}
                  />
                </div>
              </div>

              {groupId === NONE && (
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="bulk-rate">Standalone pay-as-you-go rate (Toman/GB, optional)</Label>
                  <Input
                    id="bulk-rate"
                    type="number"
                    value={ratePerGb}
                    onChange={(e) => setRatePerGb(e.target.value)}
                    placeholder="20000"
                  />
                </div>
              )}

              <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-muted/40 p-2.5 text-xs">
                <Checkbox checked={autoRenewEnabled} onCheckedChange={(v) => setAutoRenewEnabled(v === true)} className="mt-0.5" />
                <span className="flex-1">
                  <span className="font-medium">Auto-renew these accounts</span>
                  <p className="mt-0.5 text-muted-foreground">
                    Uncheck for a batch you always want to renew by hand (comp/staff/family) — none of them will ever
                    be auto-queued near quota/expiry. Changeable later per account in its inspector.
                  </p>
                </span>
              </label>

              <p className="text-xs text-muted-foreground">
                Nothing is charged. These accounts are created and (optionally) assigned — billing stays a
                separate, deliberate action.
              </p>
            </div>

            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => handleOpenChange(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={!nameInputValid || willCreate === 0 || mutation.isPending}>
                {mutation.isPending
                  ? `Creating ${willCreate}…`
                  : willCreate > 0
                    ? `Create ${willCreate} account${willCreate === 1 ? '' : 's'}`
                    : 'Create'}
              </Button>
            </DialogFooter>
          </form>
        )}

        {result && (
          <DialogFooter>
            <Button variant="outline" onClick={() => resetForm()}>
              Create another batch
            </Button>
            <Button onClick={() => handleOpenChange(false)}>Done</Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}

function NamePreview({
  enabled,
  loading,
  error,
  names,
  willCreate,
  willSkip,
}: {
  enabled: boolean
  loading: boolean
  error: string | null
  names: { index: number; marzban_username: string; already_exists: boolean }[]
  willCreate: number
  willSkip: number
}) {
  if (!enabled) {
    return (
      <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
        Enter a base name (2+ characters, letters/numbers/underscore) to see the exact usernames.
      </p>
    )
  }
  if (error) {
    return (
      <p className="flex items-start gap-1.5 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        {error}
      </p>
    )
  }
  if (loading && names.length === 0) {
    return (
      <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">
        Checking which names are free…
      </p>
    )
  }
  return (
    <div className="rounded-md border px-3 py-2">
      <div className="mb-1.5 flex items-center justify-between text-xs">
        <span className="font-medium">
          {willCreate} will be created
          {willSkip > 0 && <span className="text-warning"> · {willSkip} already taken</span>}
        </span>
        {loading && <span className="text-muted-foreground">updating…</span>}
      </div>
      <div className="flex flex-wrap gap-1">
        {names.map((n) => (
          <span
            key={n.marzban_username}
            className={
              n.already_exists
                ? 'rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-xs text-muted-foreground line-through'
                : 'rounded border bg-muted px-1.5 py-0.5 text-xs'
            }
            title={n.already_exists ? 'Already exists — will be skipped' : undefined}
          >
            {n.marzban_username}
          </span>
        ))}
      </div>
    </div>
  )
}

function BatchResult({ result, onCopy }: { result: BulkAccountResult; onCopy: (text: string) => void }) {
  return (
    <div className="flex flex-col gap-3">
      {result.warnings?.map((w) => (
        <p key={w} className="rounded-md border border-warning/40 bg-warning/10 p-2 text-xs text-foreground">
          {w}
        </p>
      ))}
      {result.customer_name && result.created > 0 && (
        <p className="text-xs text-muted-foreground">
          Owner: <span className="font-medium text-foreground">{result.customer_name}</span> — one payer for the whole batch.
        </p>
      )}
      <div className="flex flex-wrap gap-3 text-xs">
        <span className="text-success">{result.created} created</span>
        {result.skipped > 0 && <span className="text-muted-foreground">{result.skipped} skipped</span>}
        {result.failed > 0 && <span className="text-destructive">{result.failed} failed</span>}
      </div>

      {result.aborted_reason && (
        <p className="flex items-start gap-1.5 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          The batch stopped early: {result.aborted_reason}
        </p>
      )}

      {/* Said explicitly rather than left to silence — an operator who expects
          a chat full of QR codes needs to know when none are coming. */}
      <p className="text-xs text-muted-foreground">
        {result.notifications_queued
          ? 'QR messages are being sent to your Telegram chat now — one per account, plus a summary.'
          : 'No Telegram messages will be sent (BOT_TOKEN / BOT_ADMIN_CHAT_ID not configured). Copy the links below instead.'}
      </p>

      <div className="flex flex-col divide-y rounded-md border">
        {result.items.map((item) => (
          <div key={item.marzban_username} className="flex items-start gap-2 px-3 py-2">
            <span className="mt-0.5 shrink-0">
              {item.status === 'created' && <Check className="h-3.5 w-3.5 text-success" />}
              {item.status === 'created_untracked' && <AlertTriangle className="h-3.5 w-3.5 text-warning" />}
              {item.status === 'skipped_exists' && <span className="text-xs text-muted-foreground">—</span>}
              {item.status === 'failed' && <AlertTriangle className="h-3.5 w-3.5 text-destructive" />}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium">{item.marzban_username}</div>
              {item.subscription_url && (
                <div className="truncate text-xs text-muted-foreground" title={item.subscription_url}>
                  {item.subscription_url}
                </div>
              )}
              {item.error && <div className="text-xs text-muted-foreground">{item.error}</div>}
            </div>
            {item.subscription_url && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-6 shrink-0 px-1.5"
                onClick={() => onCopy(item.subscription_url!)}
                aria-label={`Copy subscription link for ${item.marzban_username}`}
              >
                <Copy className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
