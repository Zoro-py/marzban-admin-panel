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
import { cn } from '@/lib/utils'

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
 * plain Gregorian ISO string either way (`DateObject.toDate().toISOString()`
 * converts regardless of which calendar was used to pick it), so nothing
 * about the API or the stored ledger dates needs to know Jalali exists.
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
        onChange={(dateObject: DateObject | null) => setSince(dateObject ? dateObject.toDate().toISOString() : '')}
        inputClass="h-7 rounded-md border border-input bg-background px-2 text-xs w-28 outline-none focus-visible:ring-1 focus-visible:ring-ring"
        containerClassName="inline-block"
        editable={false}
      />
      {since !== '' && (
        <>
          {query.isFetching ? (
            <span className="text-muted-foreground">Loading…</span>
          ) : (
            <Money amount={query.data?.balance ?? 0} zero="settled" className="text-sm" />
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
