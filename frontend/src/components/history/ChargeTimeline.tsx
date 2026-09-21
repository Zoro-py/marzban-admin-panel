import * as React from 'react'
import { useTheme } from '@/lib/theme'
import type { ChargeHistory } from '@/lib/types'
import { formatToman, parseDate } from '@/lib/utils'
import { formatJalali } from '@/lib/jalali'
import { accountColor } from './palette'

/* One lane per account over a shared time axis — the "who was charged when,
 * and how much" picture the owner asked for. Mark encoding (NOT color — color
 * is the account's identity):
 *   circle    = charge, radius ∝ √amount (clamped)
 *   diamond   = credit (hollow), only when payments are shown
 *   tall bar  = package activated (its GB labeled when there's room)
 *   tick      = non-money event (external increase, resets, deletion…)
 * Every mark is focusable with its full story as aria-label, so the values
 * never live in shape/size alone. */

const LABEL_W = 148
const PLOT_X = LABEL_W + 12
const PLOT_W = 760
const W = PLOT_X + PLOT_W + 8
const LANE_H = 34
const PAD_T = 16
const PAD_B = 34

interface Tip {
  laneY: number
  x: number
  title: string
  rows: [string, string][]
}

interface ChargeTimelineProps {
  data: ChargeHistory
  sinceMs: number
  untilMs: number
}

/** Every stored timestamp is naive UTC; slicing the raw ISO string (an
 * earlier version of this file did) reads the UTC calendar date/time
 * literally, which is wrong here — the rest of this panel treats the
 * operator's browser-local clock as Tehran (see BalanceSinceControl's own
 * "picking Aug 17 in a Tehran browser" convention) and expects the same.
 * `parseDate` already builds the correct Date instant; this just formats it
 * with the browser's LOCAL getters instead of re-reading the UTC string. */
function fmtDateTime(iso: string): string {
  const d = parseDate(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  const time = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${date} ${time}`
}

function fmtDateOnly(iso: string): string {
  const d = parseDate(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function ChargeTimeline({ data, sinceMs, untilMs }: ChargeTimelineProps) {
  const { resolved } = useTheme()
  const [tip, setTip] = React.useState<Tip | null>(null)

  const span = Math.max(1, untilMs - sinceMs)
  // Deliberately NOT clamped: a point outside [sinceMs, untilMs] is filtered
  // out at each call site below instead of being drawn stacked on the plot
  // edge (clamping made an out-of-window point look like a real one AT the
  // boundary, which is a different, wrong fact). The two windows can
  // legitimately disagree by a few hours — the backend's date-only `until`
  // is extended to literal UTC day-end, while this axis is the browser's
  // local (Tehran) day — so this guard is not just defensive.
  const inWindow = (t: number) => t >= sinceMs && t <= untilMs
  const x = (t: number) => PLOT_X + ((t - sinceMs) / span) * PLOT_W
  const height = PAD_T + data.accounts.length * LANE_H + PAD_B

  const maxAmount = Math.max(1, ...data.entries.filter((e) => e.type === 'charge').map((e) => e.amount))
  const radius = (amount: number) => 3 + 7 * Math.sqrt(amount / maxAmount)

  const byAccount = React.useMemo(() => {
    const m = new Map<number, { entries: typeof data.entries; packages: typeof data.packages; markers: typeof data.markers }>()
    for (const a of data.accounts) m.set(a.id, { entries: [], packages: [], markers: [] })
    for (const e of data.entries) m.get(e.account_id)?.entries.push(e)
    for (const p of data.packages) m.get(p.account_id)?.packages.push(p)
    for (const k of data.markers) m.get(k.account_id)?.markers.push(k)
    return m
  }, [data])

  function tipFor(base: Omit<Tip, 'x' | 'laneY'>, tMs: number, laneCenter: number): Tip {
    return { ...base, x: x(tMs), laneY: laneCenter }
  }

  return (
    <div className="overflow-x-auto">
      <div className="relative min-w-[640px]">
        <svg viewBox={`0 0 ${W} ${height}`} className="w-full" role="img" aria-label="Charge timeline — one lane per account">
          {data.accounts.map((a, i) => {
            const color = accountColor(resolved, i)
            const laneTop = PAD_T + i * LANE_H
            const cy = laneTop + LANE_H / 2 - 3
            const bag = byAccount.get(a.id)
            const hasAny = (bag?.entries.length ?? 0) + (bag?.packages.length ?? 0) + (bag?.markers.length ?? 0) > 0
            let lastPkgLabelX = -Infinity
            return (
              <g key={a.id}>
                <text x={8} y={cy + 3.5} className="fill-foreground font-mono" fontSize={11}>
                  {a.username.length > 18 ? a.username.slice(0, 17) + '…' : a.username}
                  {a.deleted && <tspan className="fill-warning"> †</tspan>}
                </text>
                <line x1={PLOT_X} y1={cy} x2={PLOT_X + PLOT_W} y2={cy} className="stroke-border" strokeWidth={1} />
                {!hasAny && (
                  <text x={PLOT_X + 8} y={cy - 6} className="fill-muted-foreground" fontSize={9.5}>
                    no charges in this period
                  </text>
                )}

                {bag?.packages.filter((p) => inWindow(parseDate(p.activated_at).getTime())).map((p, pi) => {
                  const t = parseDate(p.activated_at).getTime()
                  const px = x(t)
                  const key = `p${a.id}-${pi}`
                  const label = `${p.data_limit_gb}GB`
                  const showLabel = px - lastPkgLabelX > 34
                  if (showLabel) lastPkgLabelX = px
                  return (
                    <g
                      key={key}
                      tabIndex={0}
                      role="img"
                      aria-label={`${a.username}: package activated ${label} for ${p.duration_days} days on ${fmtDateOnly(p.activated_at)}`}
                      onMouseEnter={() =>
                        setTip(
                          tipFor(
                            {
                              title: `${a.username} — package activated`,
                              rows: [
                                ['Date', `${fmtDateTime(p.activated_at)} · ${formatJalali(parseDate(p.activated_at))}`],
                                ['Package', `${p.data_limit_gb} GB / ${p.duration_days} days`],
                              ],
                            },
                            t,
                            cy,
                          ),
                        )
                      }
                      onMouseLeave={() => setTip(null)}
                      onFocus={() =>
                        setTip(
                          tipFor(
                            {
                              title: `${a.username} — package activated`,
                              rows: [
                                ['Date', `${fmtDateTime(p.activated_at)} · ${formatJalali(parseDate(p.activated_at))}`],
                                ['Package', `${p.data_limit_gb} GB / ${p.duration_days} days`],
                              ],
                            },
                            t,
                            cy,
                          ),
                        )
                      }
                      onBlur={() => setTip(null)}
                    >
                      <rect x={px - 1.5} y={cy - 13} width={3} height={26} rx={1} fill={color} opacity={0.85} />
                      {showLabel && (
                        <text x={px} y={cy - 16} textAnchor="middle" className="fill-muted-foreground" fontSize={9}>
                          {label}
                        </text>
                      )}
                      {/* generous hit target */}
                      <rect x={px - 8} y={cy - 16} width={16} height={32} fill="transparent" />
                    </g>
                  )
                })}

                {bag?.entries.filter((e) => inWindow(parseDate(e.date).getTime())).map((e) => {
                  const t = parseDate(e.date).getTime()
                  const isCharge = e.type === 'charge'
                  const r = isCharge ? radius(e.amount) : 5
                  const gbTxt = e.gb_amount != null ? `${e.gb_amount} GB` : 'GB —'
                  const aria = `${a.username} ${e.type} ${formatToman(e.amount)} on ${fmtDateTime(e.date)} (${formatJalali(
                    parseDate(e.date),
                  )}), ${gbTxt}, source ${e.source}${e.created_by ? `, by ${e.created_by}` : ''}${e.note ? `, ${e.note}` : ''}`
                  const handlers = {
                    onMouseEnter: () => setTip(tipFor(mkTip(a.username, e), t, cy)),
                    onMouseLeave: () => setTip(null),
                    onFocus: () => setTip(tipFor(mkTip(a.username, e), t, cy)),
                    onBlur: () => setTip(null),
                  }
                  return isCharge ? (
                    <g key={`e${e.id}`} tabIndex={0} role="img" aria-label={aria} {...handlers}>
                      <circle cx={x(t)} cy={cy} r={r} fill={color} />
                      <rect x={x(t) - Math.max(r, 6)} y={cy - Math.max(r, 6)} width={Math.max(r, 6) * 2} height={Math.max(r, 6) * 2} fill="transparent" />
                    </g>
                  ) : (
                    <g key={`e${e.id}`} tabIndex={0} role="img" aria-label={aria} {...handlers}>
                      <path
                        d={`M ${x(t)} ${cy - r} L ${x(t) + r} ${cy} L ${x(t)} ${cy + r} L ${x(t) - r} ${cy} Z`}
                        fill="none"
                        stroke={color}
                        strokeWidth={1.6}
                      />
                      <rect x={x(t) - 7} y={cy - 7} width={14} height={14} fill="transparent" />
                    </g>
                  )
                })}

                {bag?.markers.filter((k) => inWindow(parseDate(k.date).getTime())).map((k, ki) => {
                  const t = parseDate(k.date).getTime()
                  return (
                    <g
                      key={`k${ki}-${a.id}`}
                      tabIndex={0}
                      role="img"
                      aria-label={`${a.username}: ${k.action.replace(/_/g, ' ')} on ${fmtDateOnly(k.date)} — ${k.detail}`}
                      onMouseEnter={() =>
                        setTip(
                          tipFor(
                            {
                              title: `${a.username} — ${k.action.replace(/_/g, ' ')}`,
                              rows: [
                                ['Date', `${fmtDateTime(k.date)} · ${formatJalali(parseDate(k.date))}`],
                                ['Detail', k.detail],
                              ],
                            },
                            t,
                            cy,
                          ),
                        )
                      }
                      onMouseLeave={() => setTip(null)}
                      onFocus={() =>
                        setTip(
                          tipFor(
                            {
                              title: `${a.username} — ${k.action.replace(/_/g, ' ')}`,
                              rows: [
                                ['Date', `${fmtDateTime(k.date)} · ${formatJalali(parseDate(k.date))}`],
                                ['Detail', k.detail],
                              ],
                            },
                            t,
                            cy,
                          ),
                        )
                      }
                      onBlur={() => setTip(null)}
                    >
                      <line x1={x(t)} y1={cy + 9} x2={x(t)} y2={cy + 14} className="stroke-muted-foreground" strokeWidth={1.4} />
                      <rect x={x(t) - 6} y={cy + 8} width={12} height={8} fill="transparent" />
                    </g>
                  )
                })}
              </g>
            )
          })}

          {/* shared time axis — Gregorian + Jalali on 5 ticks */}
          {Array.from({ length: 5 }, (_, i) => {
            const t = sinceMs + (span * i) / 4
            const ax = x(t)
            const d = new Date(t)
            const g = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
            return (
              <g key={`ax${i}`}>
                <line x1={ax} y1={PAD_T} x2={ax} y2={height - PAD_B + 4} className="stroke-border" strokeWidth={0.5} strokeDasharray="2 3" />
                <text x={ax} y={height - 17} textAnchor="middle" className="fill-muted-foreground" fontSize={9.5}>
                  {g}
                </text>
                <text x={ax} y={height - 6} textAnchor="middle" className="fill-muted-foreground/70" fontSize={8.5}>
                  {formatJalali(d)}
                </text>
              </g>
            )
          })}
        </svg>

        {tip && (
          <div
            className="pointer-events-none absolute z-10 max-w-[260px] -translate-x-1/2 -translate-y-full rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md"
            style={{ left: `${(tip.x / W) * 100}%`, top: `${(tip.laneY / height) * 100}%` }}
          >
            <p className="mb-1 truncate font-medium">{tip.title}</p>
            {tip.rows.map(([label, value]) => (
              <p key={label} className="whitespace-nowrap text-[11px] text-muted-foreground" title={value}>
                <span className="text-foreground">{value}</span> {label === 'Date' ? '' : label.toLowerCase()}
              </p>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function mkTip(username: string, e: ChargeHistory['entries'][number]): Omit<Tip, 'x' | 'laneY'> {
  const rows: [string, string][] = [
    ['Date', `${fmtDateTime(e.date)} · ${formatJalali(parseDate(e.date))}`],
    [e.type === 'charge' ? 'Charged' : 'Paid', formatToman(e.amount)],
    ['GB', e.gb_amount != null ? String(e.gb_amount) : '— (not recorded)'],
    ['Source', e.source + (e.created_by ? ` · ${e.created_by}` : '')],
  ]
  if (e.note) rows.push(['Note', e.note])
  return { title: `${username} — ${e.type}`, rows }
}
