import * as React from 'react'
import RawDatePicker from 'react-multi-date-picker'
import type { DateObject } from 'react-multi-date-picker'
import persian from 'react-date-object/calendars/persian'
import persian_fa from 'react-date-object/locales/persian_fa'
import gregorian from 'react-date-object/calendars/gregorian'
import gregorian_en from 'react-date-object/locales/gregorian_en'
import { CalendarRange, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatJalali, jalaliMonthStartLocal, toLocalYmd } from '@/lib/jalali'

// Same CJS-interop unwrap as RangePicker — see the note there; do not
// "simplify" it away because dev mode resolves the plain import.
const DatePicker = (RawDatePicker as unknown as { default: typeof RawDatePicker }).default

/** The list pages' date-picker (checklist 2026-09-29, the DATE-PICKER row):
 * ONE control above a list that puts a `since` window under every row's
 * owed figure at once, instead of opening the inspector per account.
 * Since-only (no until): the forward window is "up to now" by definition —
 * what does each row owe FROM this date. */
export function SincePicker({
  value,
  onChange,
  label = 'Owed since',
}: {
  value: string // local YYYY-MM-DD, '' = off
  onChange: (since: string) => void
  label?: string
}) {
  const [calendarKind, setCalendarKind] = React.useState<'jalali' | 'gregorian'>('jalali')
  const { calendar, locale } =
    calendarKind === 'jalali'
      ? { calendar: persian, locale: persian_fa }
      : { calendar: gregorian, locale: gregorian_en }

  const picked = value ? new Date(`${value}T00:00:00`) : null

  function ymd(dateObject: DateObject | null): string {
    if (!dateObject) return ''
    const d = dateObject.toDate()
    d.setHours(0, 0, 0, 0)
    return toLocalYmd(d)
  }

  function preset(daysBack: number | 'jm'): void {
    const today = new Date()
    today.setHours(0, 0, 0, 0)
    if (daysBack === 'jm') {
      onChange(toLocalYmd(jalaliMonthStartLocal(new Date())))
      return
    }
    const d = new Date(today)
    d.setDate(d.getDate() - daysBack)
    onChange(toLocalYmd(d))
  }

  const caption = value
    ? `${value}${formatJalali(picked!) ? ` · ${formatJalali(picked!)}` : ''}`
    : 'All time'

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      <CalendarRange className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <button
        type="button"
        onClick={() => onChange('')}
        aria-pressed={!value}
        className={cn(
          'rounded-md border px-2 py-1 text-[11px] transition-colors',
          !value
            ? 'border-primary/40 bg-primary/10 font-medium text-primary'
            : 'border-border text-muted-foreground hover:border-input hover:text-foreground',
        )}
      >
        All time
      </button>
      <button
        type="button"
        onClick={() => preset('jm')}
        aria-pressed={value === toLocalYmd(jalaliMonthStartLocal(new Date()))}
        className={cn(
          'rounded-md border px-2 py-1 text-[11px] transition-colors',
          value === toLocalYmd(jalaliMonthStartLocal(new Date()))
            ? 'border-primary/40 bg-primary/10 font-medium text-primary'
            : 'border-border text-muted-foreground hover:border-input hover:text-foreground',
        )}
      >
        Jalali month
      </button>
      <button
        type="button"
        onClick={() => preset(29)}
        className="rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:border-input hover:text-foreground"
      >
        30 days
      </button>
      <button
        type="button"
        onClick={() => preset(89)}
        className="rounded-md border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:border-input hover:text-foreground"
      >
        90 days
      </button>

      <div className="inline-flex rounded-md border border-border p-0.5">
        {(['jalali', 'gregorian'] as const).map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => setCalendarKind(kind)}
            className={cn(
              'rounded px-2 py-0.5 text-[11px] transition-colors',
              calendarKind === kind
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            {kind === 'jalali' ? 'شمسی' : 'Gregorian'}
          </button>
        ))}
      </div>

      <DatePicker
        key={calendarKind}
        calendar={calendar}
        locale={locale}
        calendarPosition="bottom-left"
        value={picked}
        onChange={(d: DateObject | null) => onChange(ymd(d))}
        placeholder="Since…"
        aria-label={`${label} date`}
        inputClass="h-7 rounded-md border border-input bg-background px-2 text-xs w-28 outline-none focus-visible:ring-1 focus-visible:ring-ring"
        containerClassName="inline-block"
        editable={false}
      />

      {value && (
        <button
          type="button"
          onClick={() => onChange('')}
          aria-label="Clear the since window"
          title="Clear"
          className="inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}

      <span className="text-[11px] tabular-nums text-muted-foreground" title={caption}>
        {label}: {caption}
      </span>
    </div>
  )
}
