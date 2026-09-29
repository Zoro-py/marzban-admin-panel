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
import { tr, useLang } from '@/lib/i18n'

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
function utcBoundary(since: string, lang: 'fa' | 'en'): string {
  const stamp = since.replace('T', ' ').slice(0, 16)
  return lang === 'fa' ? `از ${stamp} UTC` : `from ${stamp} UTC`
}

type BalanceData = {
  balance: number
  net_owed?: number | null
  pending_amount: number | null
  pending_gb?: number | null
  gb_charged: number | null
  gb_consumed: number | null
  charged_amount: number | null
  charged_amount_gb_known?: number | null
  charge_count?: number | null
  charge_count_with_gb?: number | null
  consumed_amount: number | null
  credited_amount: number | null
  // Window-blind real balance + what was already posted before `since`.
  // The headline renders all_time_balance + pending — the same figure the
  // page's «Owes now» card carries — never the window's net_owed; see
  // realOwedNow below.
  all_time_balance?: number | null
  carried_over_balance?: number | null
}

/** The big colored headline's amount: what the scope owes RIGHT NOW —
 * all-time posted balance + pending — by construction the exact number
 * «Owes now» shows on this page. Computing the headline from the window
 * (net_owed) instead could point the OPPOSITE way: Mokaramat_YAZDi_new
 * (2026-09-29) read "149,919 T cr" in big violet while the same page's
 * card said "Owes now 163,013 T" — a payment inside the window had
 * settled charges from before it, flipping the window negative while the
 * real all-time debt sat unchanged above it. Falls back to the window
 * figure only when an older backend omits all_time_balance (net_owed
 * already includes pending — do not add it twice). */
function realOwedNow(data: BalanceData): number {
  const pending = data.pending_amount ?? 0
  const owed = data.all_time_balance != null
    ? data.all_time_balance + pending
    : (data.net_owed ?? data.balance + pending)
  return Math.round(owed * 100) / 100
}

/** What the WINDOW's net change is made of: invoiced-in-window + not
 * invoiced yet. When nothing is carried over from before the window it
 * sums exactly to the headline and the leading "=" reads it as the
 * headline's composition (the majority case — carried_over is then
 * rounding noise at most). When a pre-window balance IS carried in, the
 * window figure no longer sums to anything visible, so it is labeled as
 * the window's net change instead of wearing "=", and it must never take
 * headline color/weight: it once read "149,919 T cr" while the account's
 * real owed figure was 163,013 T (Mokaramat_YAZDi_new, 2026-09-29). Both
 * parts always shown when the second exists — a breakdown that silently
 * omitted the unbilled package read 200,000 next to «Owes now 400,000»
 * for the same account. */
function OwedBreakdown({ balance, sumsToHeadline }: { balance: BalanceData; sumsToHeadline: boolean }) {
  const [lang] = useLang()
  const pending = balance.pending_amount ?? 0
  if (pending <= 0) return null
  return (
    <span className="whitespace-nowrap text-[11px] text-muted-foreground">
      {sumsToHeadline
        ? '= '
        : <>{tr(lang, 'تغییر این بازه:', 'window net change:')} </>}
      <span className="text-foreground">{formatToman(balance.balance)}</span> {tr(lang, 'صورت‌حساب‌شده', 'invoiced')}
      {' + '}
      <span className="text-foreground">{formatToman(pending)}</span> {tr(lang, 'هنوز صورت‌حساب‌نشده', 'not invoiced yet')}
      {balance.pending_gb != null && balance.pending_gb > 0.001 && <> ({fmtGb(balance.pending_gb)} GB)</>}
    </span>
  )
}

/** A credit landing INSIDE the picked window can actually be paying off a
 * charge from BEFORE it (the payment and the charge it settles rarely land
 * on the same date) — the window then nets negative and reads like a refund
 * owed, even though the scope's real, all-time balance is still positive.
 * The headline already shows that real, all-time figure (identical to
 * «Owes now»), so this note's only remaining job is explaining WHY the
 * window's net change below differs from it: the carried-over opening
 * balance. Only rendered when since is set and the carried-over amount
 * is non-trivial (a few Toman of rounding noise is not worth a note). */
function OpeningBalanceNote({ balance }: { balance: BalanceData | undefined }) {
  const [lang] = useLang()
  if (!balance) return null
  const carried = balance.carried_over_balance ?? 0
  const allTime = balance.all_time_balance ?? null
  if (Math.abs(carried) < 1 || allTime == null) return null
  const carriedWord = carried > 0 ? tr(lang, 'پیش‌تر بدهکار بود', 'already owed') : tr(lang, 'پیش‌تر طلبکار بود', 'already in credit')
  return (
    <span className="flex w-full items-baseline gap-1 whitespace-nowrap text-[11px] text-muted-foreground">
      {tr(lang, 'شامل', 'includes')} <span className="text-foreground">{formatToman(Math.abs(carried))}</span> {carriedWord} {tr(lang, 'مربوط به قبل از این تاریخ است', 'from before this date')}
    </span>
  )
}

function GbSummary({ balance }: { balance: BalanceData | undefined }) {
  const [lang] = useLang()
  if (!balance) return null
  const { gb_charged, gb_consumed, charged_amount, charged_amount_gb_known, charge_count, charge_count_with_gb, consumed_amount, credited_amount } = balance
  if (!charge_count && gb_charged == null && gb_consumed == null && credited_amount == null) return null
  // «5 GB charged» only covers the charges that carry a GB figure, while the
  // money is every charge — say so instead of pairing one row's GB with all
  // rows' Toman.
  const partial = charge_count != null && charge_count_with_gb != null && charge_count_with_gb < charge_count
  return (
    <span
      title={tr(
        lang,
        "GB فقط روی برخی شارژها ثبت می‌شود (قدیمی‌ها هیچ — با «—» نشان داده می‌شوند، هرگز صفر). مصرف صورت‌حساب‌شده فقط آن چیزی است که داخل همین بازه واقعاً صورتحساب شده؛ شمارندهٔ زندهٔ Marzban مجموعهٔ پیوستهٔ خودش را نگه می‌دارد.",
        "GB is only recorded on some charges (older ones carry none — shown as '—', never as 0). Billed usage covers what was actually BILLED inside this window; the live Marzban counter keeps its own continuous total.",
      )}
      className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
      {!!charge_count && (
        <span className="whitespace-nowrap">
          <span className="text-foreground">{charge_count}</span> {tr(lang, 'شارژ', 'charge')}{lang === 'en' && charge_count !== 1 ? 's' : ''}
          {charged_amount != null && <> · {formatToman(charged_amount)}</>}
          {gb_charged != null && (
            <>
              {' '}— <span className="text-foreground">{fmtGb(gb_charged)} GB</span> {tr(lang, 'ثبت‌شده', 'recorded')}
              {partial && <> {tr(lang, 'روی', 'on')} {charge_count_with_gb} {tr(lang, 'از', 'of')} {charge_count}</>}
              {charged_amount_gb_known != null && partial && <> ({formatToman(charged_amount_gb_known)})</>}
            </>
          )}
          {gb_charged == null && <> — {tr(lang, 'GB ثبت نشده', 'GB not recorded')}</>}
        </span>
      )}
      {gb_consumed != null && (
        <span className="whitespace-nowrap">
          <span className="text-foreground">{fmtGb(gb_consumed)} GB</span> {tr(lang, 'مصرف صورت‌حساب‌شده', 'billed usage')}
          {consumed_amount != null && <> ({formatToman(consumed_amount)})</>}
        </span>
      )}
      {credited_amount != null && (
        <span className="whitespace-nowrap">
          <span className="text-foreground">{formatToman(credited_amount)}</span> {tr(lang, 'پرداخت‌شده', 'credited')}
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
 *
 * The big colored figure shown once a date is picked is the scope's REAL
 * owed-now amount (all_time_balance + pending — the same number the
 * page's «Owes now» card carries, see realOwedNow), never the window's
 * net change. The window figure stays below as a labeled gray line: a
 * payment inside the window that settles a pre-window charge flips the
 * window negative, and a headline allowed to point the opposite way from
 * «Owes now» on the same page would misread a debtor as a creditor
 * (owner report 2026-09-29, Mokaramat_YAZDi_new).
 */
export function BalanceSinceControl({ scope }: { scope: Scope }) {
  const [lang] = useLang()
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
      <Label className="whitespace-nowrap text-muted-foreground">{tr(lang, 'مانده از تاریخ', 'Balance since')}</Label>

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
          <span className="whitespace-nowrap text-[11px] text-muted-foreground">{utcBoundary(since, lang)}</span>
          {query.isFetching ? (
            <span className="text-muted-foreground">{tr(lang, 'در حال بارگذاری…', 'Loading…')}</span>
          ) : (
            <>
              <Money
                amount={query.data ? realOwedNow(query.data) : 0}
                zero="settled"
                className="text-sm"
              />
              {query.data && (
                <OwedBreakdown
                  balance={query.data}
                  sumsToHeadline={Math.abs(query.data.carried_over_balance ?? 0) < 1}
                />
              )}
              <GbSummary balance={query.data} />
              <OpeningBalanceNote balance={query.data} />
            </>
          )}
          <button
            type="button"
            onClick={() => setSince('')}
            className="text-muted-foreground hover:text-foreground hover:underline"
          >
            {tr(lang, 'پاک‌کردن', 'clear')}
          </button>
        </>
      )}
      {since === '' && (
        <span className="text-muted-foreground">
          {tr(
            lang,
            'یک تاریخ بردارید — مثلاً آخرین پرداخت — تا آنچه از آن موقع جمع شده را ببینید، نه مجموع کل از همیشه.',
            "Pick a date — e.g. the last payment — to see what's accrued since then, instead of the all-time total.",
          )}
        </span>
      )}
    </div>
  )
}
