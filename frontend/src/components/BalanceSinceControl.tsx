import * as React from 'react'
import RawDatePicker from 'react-multi-date-picker'
import type { DateObject } from 'react-multi-date-picker'
import persian from 'react-date-object/calendars/persian'
import persian_fa from 'react-date-object/locales/persian_fa'
import gregorian from 'react-date-object/calendars/gregorian'
import gregorian_en from 'react-date-object/locales/gregorian_en'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ReceiptText } from 'lucide-react'
import { ledgerApi } from '@/lib/api'
import type { Statement, StatementEntry, StatementRecharge, RechargeKind } from '@/lib/types'
import { Money } from '@/components/Money'
import { cn, formatToman, parseDate } from '@/lib/utils'
import { formatJalali, jalaliMonthStartLocal } from '@/lib/jalali'
import { tr, useLang, type Lang } from '@/lib/i18n'

// react-multi-date-picker is CJS; Vite 8's Rolldown wraps the whole module as
// `.default` in production builds (React error #130 in prod only). The real
// component is one level deeper — unwrap explicitly, do not "simplify".
const DatePicker = (RawDatePicker as unknown as { default: typeof RawDatePicker }).default

type Scope = { customer_id: number } | { group_id: number } | { account_id: number }
type CalendarKind = 'jalali' | 'gregorian'
type Preset = 'last_payment' | 'jm' | '30d' | '90d' | 'all' | 'custom'

const ALL_TIME = '2000-01-01T00:00:00.000Z'

function fmtGb(gb: number | null | undefined): string {
  if (gb === null || gb === undefined) return '—'
  return `${Number(gb.toFixed(2)).toLocaleString('en-US')} GB`
}

function fmtDate(value: string, cal: CalendarKind): string {
  const d = parseDate(value)
  if (Number.isNaN(d.getTime())) return '—'
  return cal === 'jalali' ? formatJalali(d) : d.toLocaleDateString('en-CA')
}

function localMidnightIso(d: Date): string {
  const x = new Date(d)
  x.setHours(0, 0, 0, 0)
  return x.toISOString()
}

/** Resolves a preset to the `since` sent to the backend (UTC ISO). «Since
 * last payment» starts one second AFTER that payment, so the window shows
 * what has accrued since they last paid — not the payment itself. */
function sinceFor(preset: Preset, custom: string, lastPayment: string | null): string | null {
  const today = new Date()
  switch (preset) {
    case 'last_payment':
      if (!lastPayment) return null
      return new Date(parseDate(lastPayment).getTime() + 1000).toISOString()
    case 'jm':
      return localMidnightIso(jalaliMonthStartLocal(today))
    case '30d':
    case '90d': {
      const d = new Date(today)
      d.setDate(d.getDate() - (preset === '30d' ? 30 : 90))
      return localMidnightIso(d)
    }
    case 'all':
      return ALL_TIME
    case 'custom':
      return custom || null
  }
}

const KIND_LABEL: Record<RechargeKind, [string, string]> = {
  package: ['بستهٔ فعال‌شده', 'package activated'],
  panel: ['افزایش حجم از پنل', 'added from panel'],
  delegate: ['تمدید نماینده', 'delegate renewal'],
  outside_panel: ['تغییر مستقیم در مرزبان', 'changed in Marzban'],
}

/* ------------------------------------------------------------------ pieces */

/** Figures ("224,237 T", "23.97 GB", "1405/07/07") must keep their LTR order
 * inside the Persian (RTL) layout — without isolation the bidi algorithm
 * flips them to "T 224,237". */
function Num({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <bdi dir="ltr" className={cn('tabular-nums', className)}>
      {children}
    </bdi>
  )
}

function Tile({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: 'muted' }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-background/40 px-3 py-2">
      <p className="truncate text-[11px] text-muted-foreground">{label}</p>
      <p className={cn('mt-0.5 truncate text-sm font-semibold', tone === 'muted' && 'text-muted-foreground')}><Num>{value}</Num></p>
      {sub && <p className="mt-0.5 text-[10.5px] leading-snug text-muted-foreground">{sub}</p>}
    </div>
  )
}

/** The bank-statement equation, one line each — the reason this widget can
 * no longer be misread: the window's movements always sit between an
 * opening and a closing balance, and «owes now» is the last line. */
function StatementLines({ s, lang }: { s: Statement; lang: Lang }) {
  const m = s.statement
  const row = (sign: string, label: string, value: React.ReactNode, strong = false) => (
    <div className={cn('flex items-baseline justify-between gap-3 py-1', strong && 'border-t border-border pt-1.5 font-semibold')}>
      <span className="text-muted-foreground">
        <span className="inline-block w-3 font-mono text-foreground">{sign}</span> {label}
      </span>
      <Num>{value}</Num>
    </div>
  )
  return (
    <div className="rounded-md border border-border px-3 py-2 text-xs" data-testid="statement-lines">
      {row(' ', tr(lang, 'ماندهٔ ابتدای بازه', 'Opening balance'), <Money amount={m.opening_balance} kind="balance" zero="dash" />)}
      {row('+', `${tr(lang, 'شارژها', 'Charges')} (${m.charge_count})`, formatToman(m.charged))}
      {row('−', `${tr(lang, 'پرداخت‌ها', 'Payments')} (${m.payment_count})`, formatToman(m.paid))}
      {row('=', tr(lang, 'ماندهٔ ثبت‌شده', 'Posted balance'), <Money amount={m.closing_balance} kind="balance" zero="dash" />, true)}
      {s.window.is_open && row('+', tr(lang, 'هنوز صورتحساب‌نشده', 'Not invoiced yet'), <Money amount={m.pending} kind="pending" />)}
      {s.window.is_open && row('=', tr(lang, 'بدهی همین الان', 'Owes now'), <Money amount={m.owed_now} kind="balance" zero="settled" />, true)}
    </div>
  )
}

function UsageTile({ s, lang, cal }: { s: Statement; lang: Lang; cal: CalendarKind }) {
  const v = s.volume
  let sub: React.ReactNode = null
  let value: React.ReactNode = fmtGb(v.used_gb)
  switch (v.usage_status) {
    case 'complete':
      sub = tr(lang, 'اندازه‌گیری مرزبان، کل بازه', 'measured by Marzban, whole window')
      break
    case 'from_history_start':
      sub = `${tr(lang, 'فقط از', 'only from')} ${fmtDate(v.usage_from ?? v.usage_history_from, cal)} — ${tr(lang, 'قبل از آن ثبت نشده', 'not recorded before')}`
      break
    case 'before_history':
      value = '—'
      sub = `${tr(lang, 'مرزبان مصرف را فقط از', 'Marzban only recorded usage from')} ${fmtDate(v.usage_history_from, cal)}`
      break
    case 'partial':
      sub = tr(lang, `بدون ${v.usage_errors.length} اکانت (خطای مرزبان)`, `missing ${v.usage_errors.length} account(s) — Marzban error`)
      break
    case 'unavailable':
      value = '—'
      sub = tr(lang, 'مرزبان در دسترس نبود', 'Marzban unreachable')
      break
    case 'skipped':
      value = '—'
      break
  }
  return <Tile label={tr(lang, 'مصرف واقعی', 'Actually used')} value={value} sub={sub} />
}

function Utilization({ s, lang }: { s: Statement; lang: Lang }) {
  const v = s.volume
  if (v.utilization_pct === null) return null
  const pct = Math.min(100, v.utilization_pct)
  return (
    <div className="space-y-1 text-[11px]">
      <div className="flex flex-wrap justify-between gap-x-3 text-muted-foreground">
        <span>
          {tr(lang, 'از حجم شارژشده مصرف شد', 'of the recharged volume used')}: <b className="text-foreground"><Num>{v.utilization_pct}%</Num></b>
        </span>
        {v.toman_per_used_gb !== null && (
          <span>
            {tr(lang, 'هزینه به ازای هر گیگ مصرفی', 'paid per used GB')}: <b className="text-foreground"><Num>{formatToman(v.toman_per_used_gb)}</Num></b>
          </span>
        )}
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-primary/12">
        <div className={cn('h-full rounded-full', v.utilization_pct > 100 ? 'bg-warning' : 'bg-primary/80')} style={{ width: `${Math.max(2, pct)}%` }} />
      </div>
    </div>
  )
}

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false)
  return (
    <div className="rounded-md border border-border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between px-3 py-2 text-xs font-medium hover:bg-accent/40"
        aria-expanded={open}
      >
        <span>
          {title}
          {count !== undefined && <span className="ms-1 text-muted-foreground">({count})</span>}
        </span>
        <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', open && 'rotate-180')} />
      </button>
      {open && <div className="border-t border-border">{children}</div>}
    </div>
  )
}

function MonthsTable({ s, lang }: { s: Statement; lang: Lang }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px] tabular-nums">
        <thead className="text-muted-foreground">
          <tr className="[&>th]:px-2 [&>th]:py-1.5 [&>th]:text-start [&>th]:font-normal">
            <th>{tr(lang, 'ماه', 'Month')}</th>
            <th>{tr(lang, 'شارژ', 'Charged')}</th>
            <th>{tr(lang, 'پرداخت', 'Paid')}</th>
            <th>{tr(lang, 'حجم شارژشده', 'Recharged')}</th>
            <th>{tr(lang, 'مصرف', 'Used')}</th>
          </tr>
        </thead>
        <tbody>
          {[...s.months].reverse().map((m) => (
            <tr key={m.month} className="border-t border-border [&>td]:whitespace-nowrap [&>td]:px-2 [&>td]:py-1.5">
              <td className="font-mono"><Num>{m.month}</Num></td>
              <td>
                <Num>{formatToman(m.charged)}</Num>
                {m.charge_count > 0 && <Num className="text-muted-foreground"> ×{m.charge_count}</Num>}
              </td>
              <td><Num>{m.paid ? formatToman(m.paid) : '—'}</Num></td>
              <td>
                <Num>{m.recharged_gb ? fmtGb(m.recharged_gb) : '—'}</Num>
                {m.recharge_count > 0 && <Num className="text-muted-foreground"> ×{m.recharge_count}</Num>}
              </td>
              <td title={m.usage_partial_month ? tr(lang, 'فقط بخشی از ماه ثبت شده', 'only part of this month recorded') : undefined}>
                {m.used_gb === null ? <span className="text-muted-foreground">{tr(lang, 'ثبت نشده', 'n/a')}</span> : <Num>{fmtGb(m.used_gb)}</Num>}
                {m.usage_partial_month && m.used_gb !== null && <span className="text-warning">*</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AccountsTable({ s, lang }: { s: Statement; lang: Lang }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[11px] tabular-nums">
        <thead className="text-muted-foreground">
          <tr className="[&>th]:px-2 [&>th]:py-1.5 [&>th]:text-start [&>th]:font-normal">
            <th>{tr(lang, 'اکانت', 'Account')}</th>
            <th>{tr(lang, 'شارژ', 'Charged')}</th>
            <th>{tr(lang, 'پرداخت', 'Paid')}</th>
            <th>{tr(lang, 'حجم شارژشده', 'Recharged')}</th>
            <th>{tr(lang, 'مصرف', 'Used')}</th>
          </tr>
        </thead>
        <tbody>
          {s.accounts.map((a) => (
            <tr key={a.account_id} className={cn('border-t border-border [&>td]:whitespace-nowrap [&>td]:px-2 [&>td]:py-1.5', a.deleted && 'opacity-60')}>
              <td className="font-mono">
                {a.username}
                <span className="ms-1 text-[10px] text-muted-foreground">{a.billing_mode}</span>
              </td>
              <td>
                <Num>{formatToman(a.charged)}</Num>
                {a.charge_count > 0 && <Num className="text-muted-foreground"> ×{a.charge_count}</Num>}
              </td>
              <td><Num>{a.paid ? formatToman(a.paid) : '—'}</Num></td>
              <td><Num>{a.recharged_gb ? `${fmtGb(a.recharged_gb)} ×${a.recharge_count}` : '—'}</Num></td>
              <td><Num>{fmtGb(a.used_gb)}</Num></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

type Activity =
  | { key: string; date: string; kind: 'money'; e: StatementEntry }
  | { key: string; date: string; kind: 'volume'; r: StatementRecharge }

function ActivityList({ s, lang, cal }: { s: Statement; lang: Lang; cal: CalendarKind }) {
  const items: Activity[] = [
    ...s.entries.map((e) => ({ key: `e${e.id}`, date: e.date, kind: 'money' as const, e })),
    ...s.recharges.map((r, i) => ({ key: `r${i}`, date: r.date, kind: 'volume' as const, r })),
  ].sort((a, b) => parseDate(b.date).getTime() - parseDate(a.date).getTime())
  const multi = s.scope.type !== 'account'
  if (!items.length) {
    return <p className="px-3 py-3 text-xs text-muted-foreground">{tr(lang, 'در این بازه تراکنشی نیست.', 'Nothing in this window.')}</p>
  }
  return (
    <ul className="max-h-80 divide-y divide-border overflow-y-auto text-[11px]">
      {items.map((it) => (
        <li key={it.key} className="flex items-start justify-between gap-3 px-3 py-1.5">
          <div className="min-w-0">
            <p className="text-muted-foreground">
              <Num>{fmtDate(it.date, cal)}</Num>
              {multi && <span className="ms-1 font-mono text-foreground/80">{it.kind === 'money' ? it.e.account ?? '' : it.r.account}</span>}
            </p>
            {it.kind === 'money' ? (
              <p className="truncate" title={it.e.note ?? undefined}>
                {it.e.type === 'charge' ? tr(lang, 'شارژ', 'Charge') : tr(lang, 'پرداخت', 'Payment')}
                {it.e.gb_amount !== null && <span className="text-muted-foreground"> · <Num>{fmtGb(it.e.gb_amount)}</Num></span>}
                {it.e.note && <span className="text-muted-foreground"> · {it.e.note}</span>}
              </p>
            ) : (
              <p className="truncate">
                {tr(lang, ...KIND_LABEL[it.r.kind])} <span className="text-muted-foreground">· <Num>{fmtGb(it.r.gb)}</Num></span>
                {it.r.days ? <span className="text-muted-foreground"> / {it.r.days}d</span> : null}
              </p>
            )}
          </div>
          {it.kind === 'money' ? (
            <div className="shrink-0 text-end tabular-nums">
              <p className={cn('font-medium', it.e.type === 'credit' ? 'text-success' : 'text-foreground')}>
                <Num>
                  {it.e.type === 'credit' ? '−' : '+'}
                  {formatToman(it.e.amount)}
                </Num>
              </p>
              <p className="text-[10px] text-muted-foreground">
                {tr(lang, 'مانده', 'bal.')} <Num><Money amount={it.e.balance_after} kind="balance" zero="dash" className="text-[10px]" /></Num>
              </p>
            </div>
          ) : (
            <Num className="shrink-0 font-medium text-primary">+{fmtGb(it.r.gb)}</Num>
          )}
        </li>
      ))}
    </ul>
  )
}

/* ------------------------------------------------------------- the widget */

/**
 * «Statement» — the operator's answer to: how much was charged, how much was
 * paid, how much volume was added and how much of it was actually used, how
 * many times, and what they owe right now. Backed by
 * GET /api/ledger/statement (backend/app/statement.py): money from the same
 * single-owner ledger roll-up as «Owes now» (the last statement line IS that
 * number), volume from QueuedPlan/AccountEvent, consumption from Marzban's own
 * per-user usage history — only from the date that history is trustworthy,
 * shown as "not recorded" before it, never as 0.
 *
 * Mounted on the account inspector (400px wide), customer and group pages,
 * and the Finance balance lookup — same props as the old control: one scope.
 */
export function BalanceSinceControl({ scope }: { scope: Scope }) {
  const [lang] = useLang()
  const [cal, setCal] = React.useState<CalendarKind>('jalali')
  const [preset, setPreset] = React.useState<Preset>('last_payment')
  const [custom, setCustom] = React.useState('')

  // Money-only all-time pass: instant (no Marzban), and tells us when the
  // last payment was — the default window.
  const meta = useQuery({
    queryKey: ['ledger', 'statement', scope, ALL_TIME, false],
    queryFn: () => ledgerApi.statement({ ...scope, since: ALL_TIME, usage: false }),
    staleTime: 60_000,
  })
  const lastPayment = meta.data?.last_payment_at ?? null
  const effectivePreset: Preset = preset === 'last_payment' && meta.isSuccess && !lastPayment ? 'all' : preset
  const since = sinceFor(effectivePreset, custom, lastPayment)

  const query = useQuery({
    queryKey: ['ledger', 'statement', scope, since, true],
    queryFn: () => ledgerApi.statement({ ...scope, since: since!, usage: true }),
    enabled: since !== null,
    staleTime: 60_000,
  })
  const s = query.data

  const presets: [Preset, string, string][] = [
    ['last_payment', 'از آخرین پرداخت', 'Since last payment'],
    ['jm', 'این ماه شمسی', 'This Shamsi month'],
    ['30d', '۳۰ روز', '30 days'],
    ['90d', '۹۰ روز', '90 days'],
    ['all', 'از ابتدا', 'All time'],
  ]
  const { calendar, locale } = cal === 'jalali' ? { calendar: persian, locale: persian_fa } : { calendar: gregorian, locale: gregorian_en }

  return (
    <div dir={lang === 'fa' ? 'rtl' : 'ltr'} className="space-y-2.5 rounded-lg border border-border bg-card px-3 py-3 text-xs" data-testid="statement-widget">
      {/* header */}
      <div className="flex flex-wrap items-center gap-2">
        <ReceiptText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="font-semibold">{tr(lang, 'صورت‌حساب', 'Statement')}</span>
        {s && (
          <span className="text-[11px] tabular-nums text-muted-foreground">
            {s.window.since.startsWith('2000-01-01') ? tr(lang, 'از ابتدا', 'from the start') : <Num>{fmtDate(s.window.since, cal)}</Num>}
            {lang === 'fa' ? ' ← ' : ' → '}
            {s.window.is_open ? tr(lang, 'الان', 'now') : <Num>{fmtDate(s.window.until, cal)}</Num>}
          </span>
        )}
        <div className="ms-auto inline-flex rounded-md border border-border p-0.5">
          {(['jalali', 'gregorian'] as CalendarKind[]).map((k) => (
            <button
              key={k}
              type="button"
              onClick={() => setCal(k)}
              className={cn('rounded px-1.5 py-0.5 text-[10.5px]', cal === k ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}
            >
              {k === 'jalali' ? 'شمسی' : 'Greg.'}
            </button>
          ))}
        </div>
      </div>

      {/* window presets + custom date */}
      <div className="flex flex-wrap items-center gap-1.5">
        {presets.map(([p, fa, en]) => (
          <button
            key={p}
            type="button"
            onClick={() => setPreset(p)}
            disabled={p === 'last_payment' && meta.isSuccess && !lastPayment}
            title={p === 'last_payment' && lastPayment ? fmtDate(lastPayment, cal) : undefined}
            className={cn(
              'rounded-full border px-2 py-0.5 text-[11px] transition-colors disabled:opacity-40',
              effectivePreset === p ? 'border-primary bg-primary/10 text-foreground' : 'border-border text-muted-foreground hover:text-foreground',
            )}
          >
            {tr(lang, fa, en)}
            {p === 'last_payment' && lastPayment && <Num className="ms-1 text-muted-foreground">{fmtDate(lastPayment, cal)}</Num>}
          </button>
        ))}
        <DatePicker
          key={cal}
          calendar={calendar}
          locale={locale}
          calendarPosition="bottom-right"
          value={preset === 'custom' && custom ? new Date(custom) : null}
          onChange={(d: DateObject | null) => {
            if (!d) return
            setCustom(localMidnightIso(d.toDate()))
            setPreset('custom')
          }}
          placeholder={tr(lang, 'تاریخ دلخواه…', 'Pick a date…')}
          inputClass={cn(
            'h-6 w-28 rounded-full border bg-background px-2 text-[11px] outline-none focus-visible:ring-1 focus-visible:ring-ring',
            preset === 'custom' ? 'border-primary' : 'border-border',
          )}
          containerClassName="inline-block"
          editable={false}
        />
      </div>

      {!s && (query.isLoading || meta.isLoading) && <p className="text-muted-foreground">{tr(lang, 'در حال بارگذاری…', 'Loading…')}</p>}
      {(query.isError || meta.isError) && <p className="text-destructive">{tr(lang, 'خطا در دریافت صورت‌حساب', 'Could not load the statement')}</p>}

      {s && (
        <div className={cn('space-y-2.5 transition-opacity', query.isFetching && 'opacity-60')}>
          {/* headline: the real owed-now, identical to «Owes now» */}
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-muted-foreground">{tr(lang, 'بدهی همین الان', 'Owes now')}</span>
            <Num><Money amount={s.statement.owed_now} kind="balance" zero="settled" className="text-lg" /></Num>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <Tile
              label={tr(lang, 'شارژ شده (صورتحساب)', 'Charged (invoiced)')}
              value={formatToman(s.statement.charged)}
              sub={
                <>
                  {s.statement.charge_count} {tr(lang, 'بار', 'time(s)')}
                  {s.charges_gb.gb !== null && (
                    <>
                      {' · '}
                      {fmtGb(s.charges_gb.gb)}
                      {s.charges_gb.with_gb < s.charges_gb.of && ` (${tr(lang, 'ثبت روی', 'on')} ${s.charges_gb.with_gb}/${s.charges_gb.of})`}
                    </>
                  )}
                </>
              }
            />
            <Tile
              label={tr(lang, 'پرداخت‌شده', 'Paid')}
              value={formatToman(s.statement.paid)}
              sub={`${s.statement.payment_count} ${tr(lang, 'بار', 'time(s)')}`}
            />
            <Tile
              label={tr(lang, 'حجم شارژشده', 'Volume recharged')}
              value={s.volume.recharge_count ? fmtGb(s.volume.recharged_gb) : '—'}
              tone={s.volume.recharge_count ? undefined : 'muted'}
              sub={
                s.volume.recharge_count
                  ? `${s.volume.recharge_count} ${tr(lang, 'بار', 'time(s)')}` +
                    (Object.entries(s.volume.by_kind) as [RechargeKind, { gb: number; count: number }][])
                      .filter(([, k]) => k.count)
                      .map(([kind, k]) => ` · ${tr(lang, ...KIND_LABEL[kind])} ${k.count}`)
                      .join('')
                  : s.scope.billing_modes.length === 1 && s.scope.billing_modes[0] === 'payg'
                    ? tr(lang, 'مصرفی (payg) — شارژ حجمی ندارد', 'pay-as-you-go — billed by usage')
                    : tr(lang, 'در این بازه شارژی نشده', 'none in this window')
              }
            />
            <UsageTile s={s} lang={lang} cal={cal} />
          </div>

          <Utilization s={s} lang={lang} />
          <StatementLines s={s} lang={lang} />

          {s.volume.excluded.length > 0 && (
            <p className="text-[10.5px] leading-snug text-muted-foreground">
              {tr(lang, 'در «حجم شارژشده» حساب نشده', 'Not counted as recharged')}:{' '}
              {s.volume.excluded
                .map((r) => `${r.account} ${r.gb > 0 ? '+' : ''}${fmtGb(r.gb)} (${r.gb < 0 ? tr(lang, 'کاهش', 'reduction') : tr(lang, 'تغییر سقف payg', 'payg cap change')})`)
                .join(lang === 'fa' ? '، ' : ', ')}
            </p>
          )}

          {s.months.length > 1 && (
            <Section title={tr(lang, 'ماه‌به‌ماه (شمسی)', 'By Shamsi month')} count={s.months.length}>
              <MonthsTable s={s} lang={lang} />
            </Section>
          )}
          {s.scope.type !== 'account' && s.accounts.length > 0 && (
            <Section title={tr(lang, 'به تفکیک اکانت', 'By account')} count={s.accounts.length}>
              <AccountsTable s={s} lang={lang} />
            </Section>
          )}
          <Section title={tr(lang, 'ریز تراکنش‌ها', 'All activity')} count={s.entries.length + s.recharges.length}>
            <ActivityList s={s} lang={lang} cal={cal} />
            {s.entries_truncated && (
              <p className="border-t border-border px-3 py-1.5 text-[10.5px] text-warning">
                {tr(lang, 'فقط ۵۰۰ تراکنش اول نشان داده شده — بازهٔ کوتاه‌تری بگیرید.', 'Only the first 500 entries are shown — narrow the window.')}
              </p>
            )}
          </Section>
        </div>
      )}
    </div>
  )
}
