import * as React from 'react'
import { Check, ChevronDown, Search, Users, X } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { HistoryAccount } from '@/lib/types'
import { cn } from '@/lib/utils'

export const MAX_ACCOUNTS = 50

interface AccountPickerProps {
  options: HistoryAccount[]
  selected: number[]
  onChange: (ids: number[]) => void
}

/** Multi-select for the history view's account lanes. Lists EVERY account
 * including soft-deleted ones (their ledger history outlives them — the
 * picker's data source is /api/history/accounts for exactly that reason),
 * searches across username/customer/group, and offers "all accounts of
 * customer/group" shortcuts so a family's N accounts are one click, not N. */
export function AccountPicker({ options, selected, onChange }: AccountPickerProps) {
  const [open, setOpen] = React.useState(false)
  const [query, setQuery] = React.useState('')
  const [capNote, setCapNote] = React.useState(false)

  const byId = React.useMemo(() => new Map(options.map((a) => [a.id, a])), [options])
  const selectedSet = React.useMemo(() => new Set(selected), [selected])

  const q = query.trim().toLowerCase()
  const filtered = React.useMemo(() => {
    if (!q) return options
    return options.filter(
      (a) =>
        a.username.toLowerCase().includes(q) ||
        (a.customer_name?.toLowerCase().includes(q) ?? false) ||
        (a.group_name?.toLowerCase().includes(q) ?? false),
    )
  }, [options, q])

  // Alive accounts first, then deleted (still selectable, just out of the way).
  const ordered = React.useMemo(
    () => [...filtered].sort((a, b) => Number(a.deleted) - Number(b.deleted) || a.username.localeCompare(b.username)),
    [filtered],
  )

  // Shortcuts: every customer/group owning 2+ listed accounts (a single-account
  // owner is one click away as a plain row already).
  const shortcuts = React.useMemo(() => {
    const byCustomer = new Map<string, { label: string; ids: number[] }>()
    const byGroup = new Map<string, { label: string; ids: number[] }>()
    for (const a of options) {
      if (a.customer_name != null) {
        const key = `c${a.customer_id}`
        const slot = byCustomer.get(key) ?? { label: a.customer_name, ids: [] }
        slot.ids.push(a.id)
        byCustomer.set(key, slot)
      }
      if (a.group_name != null) {
        const key = `g${a.group_id}`
        const slot = byGroup.get(key) ?? { label: a.group_name, ids: [] }
        slot.ids.push(a.id)
        byGroup.set(key, slot)
      }
    }
    return [
      ...[...byCustomer.entries()].filter(([, v]) => v.ids.length >= 2).map(([k, v]) => ({ key: k, kind: 'customer' as const, ...v })),
      ...[...byGroup.entries()].filter(([, v]) => v.ids.length >= 2).map(([k, v]) => ({ key: k, kind: 'group' as const, ...v })),
    ].sort((a, b) => a.label.localeCompare(b.label))
  }, [options])

  function toggle(id: number) {
    setCapNote(false)
    if (selectedSet.has(id)) {
      onChange(selected.filter((x) => x !== id))
    } else if (selected.length < MAX_ACCOUNTS) {
      onChange([...selected, id])
    } else {
      setCapNote(true)
    }
  }

  function addMany(ids: number[]) {
    const next = [...selected]
    let capped = false
    for (const id of ids) {
      if (next.length >= MAX_ACCOUNTS) {
        capped = true
        break
      }
      if (!next.includes(id)) next.push(id)
    }
    setCapNote(capped)
    if (next.length !== selected.length) onChange(next)
  }

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Popover open={open} onOpenChange={(o) => { setOpen(o); if (!o) setQuery('') }}>
        <PopoverTrigger asChild>
          <Button variant="outline" size="sm" className="h-8 justify-start gap-2 font-normal">
            <Users className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <span className="truncate">
              {selected.length === 0 ? 'Select accounts…' : `${selected.length} account${selected.length > 1 ? 's' : ''} selected`}
            </span>
            <ChevronDown className="ml-auto h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-[340px] p-0">
          <div className="flex items-center gap-2 border-b border-border px-2.5 py-2">
            <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <Input
              autoFocus
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search account, customer, group…"
              className="h-7 border-0 shadow-none focus-visible:ring-0"
            />
          </div>

          {!q && shortcuts.length > 0 && (
            <div className="border-b border-border px-2.5 py-2">
              <p className="mb-1.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">Add all at once</p>
              <div className="flex max-h-24 flex-wrap gap-1 overflow-y-auto">
                {shortcuts.map((s) => (
                  <button
                    key={s.key}
                    type="button"
                    onClick={() => addMany(s.ids)}
                    className="rounded-md border border-border px-1.5 py-px text-[11px] text-muted-foreground transition-colors hover:border-primary/40 hover:text-foreground"
                  >
                    +{s.ids.length} {s.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="max-h-72 overflow-y-auto py-1">
            {ordered.length === 0 && (
              <p className="px-3 py-4 text-center text-xs text-muted-foreground">No accounts match “{query}”.</p>
            )}
            {ordered.map((a) => {
              const isSel = selectedSet.has(a.id)
              return (
                <button
                  key={a.id}
                  type="button"
                  onClick={() => toggle(a.id)}
                  className={cn(
                    'flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-xs transition-colors hover:bg-accent/60',
                    isSel && 'bg-accent/40',
                  )}
                >
                  <span
                    className={cn(
                      'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                      isSel ? 'border-primary bg-primary text-primary-foreground' : 'border-input',
                    )}
                  >
                    {isSel && <Check className="h-2.5 w-2.5" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate font-mono text-[12px] leading-tight">{a.username}</span>
                    <span className="block truncate text-[11px] leading-tight text-muted-foreground">
                      {[a.customer_name, a.group_name].filter(Boolean).join(' · ') || 'unassigned'}
                    </span>
                  </span>
                  {a.deleted && (
                    <Badge variant="warning" className="shrink-0">
                      deleted
                    </Badge>
                  )}
                </button>
              )
            })}
          </div>

          {capNote && (
            <p className="border-t border-border px-3 py-1.5 text-[11px] text-warning">
              Selection is capped at {MAX_ACCOUNTS} accounts.
            </p>
          )}
        </PopoverContent>
      </Popover>

      {selected.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {selected.map((id) => {
            const a = byId.get(id)
            return (
              <span
                key={id}
                className="inline-flex max-w-full items-center gap-1 rounded-md border border-border bg-muted/50 pl-1.5 pr-0.5 py-px text-[11px]"
              >
                <span className="truncate font-mono">{a?.username ?? `#${id}`}</span>
                {a?.deleted && <span className="text-warning">(deleted)</span>}
                <button
                  type="button"
                  aria-label={`Remove ${a?.username ?? id}`}
                  onClick={() => onChange(selected.filter((x) => x !== id))}
                  className="rounded p-0.5 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            )
          })}
          <button
            type="button"
            onClick={() => onChange([])}
            className="self-center px-1 text-[11px] text-muted-foreground transition-colors hover:text-foreground hover:underline"
          >
            clear
          </button>
        </div>
      )}
    </div>
  )
}
