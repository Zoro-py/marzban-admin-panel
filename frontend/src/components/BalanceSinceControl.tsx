import * as React from 'react'
import RawDatePicker from 'react-multi-date-picker'
import type { DateObject } from 'react-multi-date-picker'
import persian from 'react-date-object/calendars/persian'
import persian_fa from 'react-date-object/locales/persian_fa'
import gregorian from 'react-date-object/calendars/gregorian'
import gregorian_en from 'react-date-object/locales/gregorian_en'
import { useQuery } from '@tanstack/react-query'
import { CalendarClock } from 'lucide-react'
import { ledgerApi } from '@/lib/api'
import { Label } from '@/components/ui/label'
import { Money } from '@/components/Money'
import { cn, formatToman } from '@/lib/utils'

// react-multi-date-picker ships CJS with BOTH `exports.__esModule = true`
// and its own `exports.default`. Vite 8's Rolldown bundler compiles a
// default import from a CJS package in unconditional "Node mode" — which
// wraps the WHOLE module as `.default`, ignoring the package's own
// `.default`, because @rollup/plugin-commonjs-style `__esModule` detection
// isn't what Rolldown's interop checks. The result: `RawDatePicker` above
// is the *module object*, not the component, so `<RawDatePicker />` threw
// React error #130 ("Element type is invalid... got: object") — only in
// the production build; dev's separate esbuild pre-bundling path resolves
// the same import correctly, which is why this wasn't caught until it
// shipped. Confirmed by replaying the bundler's own __toESM/__copyProps
// helpers (extracted from an unminified prod build) against the installed
// package directly: `wrapped.default === mod` (the whole module), and the
// real component — a React.forwardRef object — is one level deeper, at
// `mod.default`. Unwrap it explicitly rather than trusting the interop.
const DatePicker = (RawDatePicker as unknown as { default: typeof RawDatePicker }).default

type Scope = { customer_id: number } | { group_id: number } | { account_id: number }
type CalendarKind = 'jalali' | 'gregorian'

// GB figures read like the operator talks about them: "40 GB charged,
// 33 GB consumed", each with its Toman equivalent. Trim trailing zeros
// (18.00 -> 18) so it stays compact; "—" when the backend reports null
// (charges that predate GB tracking carry no honest GB figure — never
// rendered as 0). Each metric is its own nowrap span so the row wraps
// BETWEEN metrics on a phone instead of overflowing.
function fmtGb(gb: number): string {
  return String(Number(gb.toFixed(2)))
}

// The picked date is sent as browser-local midnight converted to UTC —
// picking "Aug 17" in a Tehran browser sends "2026-08-16T20:30:00.000Z",
// which legitimately includes ledger rows the raw DB shows as "2026-08-16".
// Always show the actual UTC boundary next to the picked date so the window
// is never silently surprising when cross-checked against the ledger.
function utcBoundary(since: string): string {
  return `from ${since.replace('T', ' ').slice(0, 16)} UTC`
}

function GbSummary({ balance }: {
  balance: {
    gb_charged: number | null
    gb_consumed: number | null
    charged_amount: number | null
    consumed_amount: number | null
    credited_amount: number | null
    gb_pending: number | null
    pending_amount: number | null
  } | undefined
}) {
  if (!balance) return null
  const { gb_charged, gb_consumed, charged_amount, consumed_amount, credited_amount, gb_pending, pending_amount } = balance
  if (gb_charged == null && gb_consumed == null && charged_amount == null && consumed_amount == null && credited_amount == null && !gb_pending) {
    return null
  }
  return (
    <span
      title="Usage rows cover what was actually BILLED inside this window — the live Marzban counter keeps its own continuous total and is not summed here."
      className="flex flex-wrap items-center gap-x-1 gap-y-0.5 text-[11px] text-muted-foreground">
      <span className="whitespace-nowrap">
        <span className="text-foreground">{gb_charged != null ? `${fmtGb(gb_charged)} GB` : '—'}</span>
        {' '}charged
        {charged_amount != null && <> ({formatToman(charged_amount)})</>}
      </span>
      <span className="whitespace-nowrap">
        <span className="text-foreground">{gb_consumed != null ? `${fmtGb(gb_consumed)} GB` : '—'}</span>
        {' '}billed usage
        {consumed_amount != null && <> ({formatToman(consumed_amount)})</>}
      </span>
      {credited_amount != null && (
        <span className="whitespace-nowrap">
          <span className="text-foreground">{formatToman(credited_amount)}</span> credited
        </span>
      )}
      {gb_pending != null && gb_pending > 0.001 && (
        <span className="whitespace-nowrap">
          {fmtGb(gb_pending)} GB accruing
          {pending_amount != null && <> ({formatToman(pending_amount)})</>}
        </span>
      )}
    </span>
  )
}

const CALENDARS: Record<CalendarKind, { calendar: typeof persian; locale: typeof persian_fa; label: string }> = {
  jalali: { calendar: persian, locale: persian_fa, label: 'شمسی' },
  gregorian: { calendar: gregorian, locale: gregorian_en, label: 'Gregorian' },
}

/**
 * "What do they owe FROM this date forward" — e.g. the date of their last
 * payment, so a running balance doesn't quietly drift out of sight between
 * manual reconciliations. One control, reused on the customer detail page
 * and the account inspector — same scope shape the backend's
 * GET /api/ledger/balance already accepts (customer_id XOR group_id XOR
 * account_id), just picking which one this instance sends.
 *
 * The calendar shown for picking can be switched between Jalali (Shamsi)
 * and Gregorian — some operators think in one, some in the other, and
 * ledger dates themselves come from bank receipts that could be dated
 * either way. Whichever is picked, the VALUE sent to the backend is a
 * plain Gregorian ISO string either way, so nothing about the API or the
 * stored ledger dates needs to know Jalali exists.
 *
 * The value is the picked day at LOCAL MIDNIGHT (`setHours(0,0,0,0)`
 * before toISOString). react-multi-date-picker seeds its internal
 * DateObject from "now" and only swaps in the picked Y/M/D, so the raw
 * toDate() carries the current wall-clock time — picking "Aug 17" at
 * 04:03 Tehran would silently send a 00:33-UTC boundary and exclude the
 * first 4 hours of the very day the operator asked for. Zeroing the time
 * makes the boundary deterministic: local midnight of the picked day,
 * which the widget then shows verbatim as its UTC equivalent.
 */
export function BalanceSinceControl({ scope }: { scope: Scope }) {
  const [since, setSince] = React.useState('')
  const [calendarKind, setCalendarKind] = React.useState<CalendarKind>('jalali')

  const query = useQuery({
    queryKey: ['ledger', 'balance', scope, since],
    queryFn: () => ledgerApi.balance({ ...scope, since }),
    enabled: since !== '',
  })

  const { calendar, locale } = CALENDARS[calendarKind]

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card px-4 py-2.5 text-xs">
      <CalendarClock className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <Label className="whitespace-nowrap text-muted-foreground">Balance since</Label>

      <div className="inline-flex rounded-md border border-border p-0.5">
        {(Object.keys(CALENDARS) as CalendarKind[]).map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => setCalendarKind(kind)}
            className={cn(
              'rounded px-2 py-0.5 text-[11px] transition-colors',
              calendarKind === kind ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {CALENDARS[kind].label}
          </button>
        ))}
      </div>

      <DatePicker
        key={calendarKind}
        calendar={calendar}
        locale={locale}
        calendarPosition="bottom-right"
        value={since ? new Date(since) : null}
        onChange={(dateObject: DateObject | null) => {
          if (!dateObject) { setSince(''); return }
          // Local midnight of the picked day — see the docstring above for
          // why the raw toDate() must not be trusted to be midnight.
          const picked = dateObject.toDate()
          picked.setHours(0, 0, 0, 0)
          setSince(picked.toISOString())
        }}
        inputClass="h-7 rounded-md border border-input bg-background px-2 text-xs w-28 outline-none focus-visible:ring-1 focus-visible:ring-ring"
        containerClassName="inline-block"
        editable={false}
      />
      {since !== '' && (
        <>
          <span className="whitespace-nowrap text-[11px] text-muted-foreground">{utcBoundary(since)}</span>
          {query.isFetching ? (
            <span className="text-muted-foreground">Loading…</span>
          ) : (
            <>
              <Money amount={query.data?.balance ?? 0} zero="settled" className="text-sm" />
              <GbSummary balance={query.data} />
            </>
          )}
          <button
            type="button"
            onClick={() => setSince('')}
            className="text-muted-foreground hover:text-foreground hover:underline"
          >
            clear
          </button>
        </>
      )}
      {since === '' && (
        <span className="text-muted-foreground">
          Pick a date — e.g. the last payment — to see what's accrued since then, instead of the all-time total.
        </span>
      )}
    </div>
  )
}
