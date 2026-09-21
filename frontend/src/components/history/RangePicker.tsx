import * as React from 'react'
import RawDatePicker from 'react-multi-date-picker'
import type { DateObject } from 'react-multi-date-picker'
import persian from 'react-date-object/calendars/persian'
import persian_fa from 'react-date-object/locales/persian_fa'
import gregorian from 'react-date-object/calendars/gregorian'
import gregorian_en from 'react-date-object/locales/gregorian_en'
import { CalendarRange } from 'lucide-react'
import { cn, formatDate } from '@/lib/utils'
import { formatJalali, jalaliMonthStartLocal, toLocalYmd } from '@/lib/jalali'

// Same CJS-interop unwrap as BalanceSinceControl: Rolldown's production
// bundling wraps the whole react-multi-date-picker module as `.default`, so
// the real component sits one level deeper. Dev mode resolves the plain
// import — which is exactly why this must be unwrapped explicitly and not
// "fixed" when it looks redundant in dev.
const DatePicker = (RawDatePicker as unknown as { default: typeof RawDatePicker }).default

export type RangeCode = '30d' | '90d' | '6m' | 'jm' | 'all' | 'custom'

export interface RangeValue {
  code: RangeCode
  /** Local YYYY-MM-DD — only meaningful when code === 'custom'. */
  customSince: string
  customUntil: string
}

export const DEFAULT_RANGE: RangeValue = { code: '6m', customSince: '', customUntil: '' }

const PRESETS: { code: RangeCode; label: string }[] = [
  { code: '30d', label: '30 days' },
  { code: '90d', label: '90 days' },
  { code: '6m', label: '6 months' },
  { code: 'jm', label: 'Jalali month' },
  { code: 'all', label: 'All' },
  { code: 'custom', label: 'Custom' },
]

type CalendarKind = 'jalali' | 'gregorian'
const CALENDARS: Record<CalendarKind, { calendar: typeof persian; locale: typeof persian_fa; label: string }> = {
  jalali: { calendar: persian, locale: persian_fa, label: 'شمسی' },
  gregorian: { calendar: gregorian, locale: gregorian_en, label: 'Gregorian' },
}

function addDays(date: Date, days: number): Date {
  const d = new Date(date)
  d.setDate(d.getDate() + days)
  return d
}

/** The concrete window a RangeValue maps to — DATE-ONLY local strings. The
 * backend reads a date-only `since` as that day's midnight and a date-only
 * `until` as through the END of that day, so presets here never need to
 * reason about times at all. */
export function resolveRange(value: RangeValue): { since: string; until: string } {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  switch (value.code) {
    case '30d':
      return { since: toLocalYmd(addDays(today, -29)), until: toLocalYmd(today) }
    case '90d':
      return { since: toLocalYmd(addDays(today, -89)), until: toLocalYmd(today) }
    case '6m':
      return { since: toLocalYmd(addDays(today, -180)), until: toLocalYmd(today) }
    case 'jm':
      return { since: toLocalYmd(jalaliMonthStartLocal(new Date())), until: toLocalYmd(today) }
    case 'all':
      // Far enough back to predate the panel itself; the ledger's oldest
      // row (2026-07) is well inside.
      return { since: '2000-01-01', until: toLocalYmd(today) }
    case 'custom':
      return { since: value.customSince, until: value.customUntil }
  }
}

export function isRangeValid(value: RangeValue): boolean {
  if (value.code !== 'custom') return true
  const YMD = /^\d{4}-\d{2}-\d{2}$/
  if (!YMD.test(value.customSince) || !YMD.test(value.customUntil)) return false
  // Format-only was not enough: an inverted pick (since > until) passed this
  // check, sent an inverted window to the backend (which 400s on it) and, in
  // the caption below, rendered a "2026-06-10 -> 2026-06-01" span as if it
  // were valid. Found by multi-model review.
  return value.customSince <= value.customUntil
}

/** The picked day is taken at LOCAL midnight and kept as a date-only string
 * (see resolveRange) — mirroring BalanceSinceControl's deterministic-midnight
 * rule without re-introducing a UTC boundary into the value. */
function ymdFromPicker(dateObject: DateObject | null): string {
  if (!dateObject) return ''
  const picked = dateObject.toDate()
  picked.setHours(0, 0, 0, 0)
  return toLocalYmd(picked)
}

interface RangePickerProps {
  value: RangeValue
  onChange: (patch: Partial<RangeValue>) => void
}

export function RangePicker({ value, onChange }: RangePickerProps) {
  const [calendarKind, setCalendarKind] = React.useState<CalendarKind>('jalali')
  const { calendar, locale } = CALENDARS[calendarKind]
  const resolved = resolveRange(value)

  const sinceDate = value.customSince ? new Date(`${value.customSince}T00:00:00`) : null
  const untilDate = value.customUntil ? new Date(`${value.customUntil}T00:00:00`) : null

  // Caption states the window in BOTH calendars — the operator checks
  // receipts against whichever one the receipt is written in.
  const caption =
    value.code === 'custom' && !isRangeValid(value)
      ? 'Pick both dates.'
      : `${resolved.since} → ${resolved.until} · ${formatJalali(new Date(`${resolved.since}T00:00:00`))} → ${formatJalali(
          new Date(`${resolved.until}T00:00:00`),
        )}`

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <CalendarRange className="mr-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        {PRESETS.map((p) => (
          <button
            key={p.code}
            type="button"
            onClick={() => onChange({ code: p.code })}
            aria-pressed={value.code === p.code}
            className={cn(
              'rounded-md border px-2 py-1 text-[11px] transition-colors',
              value.code === p.code
                ? 'border-primary/40 bg-primary/10 font-medium text-primary'
                : 'border-border text-muted-foreground hover:border-input hover:text-foreground',
            )}
          >
            {p.label}
          </button>
        ))}
      </div>

      {value.code === 'custom' && (
        <div className="flex flex-wrap items-center gap-2">
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
            key={`s-${calendarKind}`}
            calendar={calendar}
            locale={locale}
            calendarPosition="bottom-left"
            value={sinceDate}
            onChange={(d: DateObject | null) => onChange({ customSince: ymdFromPicker(d) })}
            placeholder="From"
            inputClass="h-7 rounded-md border border-input bg-background px-2 text-xs w-28 outline-none focus-visible:ring-1 focus-visible:ring-ring"
            containerClassName="inline-block"
            editable={false}
          />
          <span className="text-xs text-muted-foreground">→</span>
          <DatePicker
            key={`u-${calendarKind}`}
            calendar={calendar}
            locale={locale}
            calendarPosition="bottom-left"
            value={untilDate}
            onChange={(d: DateObject | null) => onChange({ customUntil: ymdFromPicker(d) })}
            placeholder="To"
            inputClass="h-7 rounded-md border border-input bg-background px-2 text-xs w-28 outline-none focus-visible:ring-1 focus-visible:ring-ring"
            containerClassName="inline-block"
            editable={false}
          />
        </div>
      )}

      <p className="truncate text-[11px] text-muted-foreground" title={caption}>
        {caption}
        {value.code !== 'custom' && ` (${formatDate(new Date().toISOString())} today)`}
      </p>
    </div>
  )
}
