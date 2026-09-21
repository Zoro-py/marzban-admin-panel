import * as React from 'react'
import { useTheme } from '@/lib/theme'
import type { ChargeHistory } from '@/lib/types'
import { parseDate } from '@/lib/utils'
import { accountColor } from './palette'

/* Cumulative charged Toman over the window — one step line per account plus a
 * thicker neutral "total" line. Step (not smooth): money arrives in discrete
 * posts; a slope between two charges would imply it accrued continuously.
 * Built ONLY from real rows in the window — no interpolation, no padding. */

const W = 940
const H = 250
const PAD_L = 58
const PAD_R = 130
const PAD_T = 14
const PAD_B = 26

function fmtCompact(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${Math.round(v / 1e3)}k`
  return String(Math.round(v))
}

interface Series {
  label: string
  color: string
  points: { t: number; v: number }[]  // events only; step path built after
  final: number
  total?: boolean
}

export function CumulativeChart({ data, sinceMs, untilMs }: { data: ChargeHistory; sinceMs: number; untilMs: number }) {
  const { resolved } = useTheme()
  const [tip, setTip] = React.useState<{ x: number; y: number; label: string; value: number } | null>(null)

  const span = Math.max(1, untilMs - sinceMs)
  const x = (t: number) => PAD_L + Math.min(1, Math.max(0, (t - sinceMs) / span)) * (W - PAD_L - PAD_R)

  const series = React.useMemo<Series[]>(() => {
    const out: Series[] = []
    data.accounts.forEach((a, i) => {
      const charges = data.entries
        .filter((e) => e.account_id === a.id && e.type === 'charge')
        .sort((p, q) => p.date.localeCompare(q.date))
      if (charges.length === 0) return
      let cum = 0
      const points = charges.map((e) => ({ t: parseDate(e.date).getTime(), v: (cum += e.amount) }))
      out.push({ label: a.username, color: accountColor(resolved, i), points, final: cum })
    })
    const all = [...data.entries]
      .filter((e) => e.type === 'charge')
      .sort((p, q) => p.date.localeCompare(q.date))
    if (all.length > 0) {
      let cum = 0
      const points = all.map((e) => ({ t: parseDate(e.date).getTime(), v: (cum += e.amount) }))
      out.push({ label: 'Total', color: 'foreground', points, final: cum, total: true })
    }
    return out
  }, [data, resolved])

  if (series.length === 0) {
    return (
      <div className="flex h-[200px] items-center justify-center rounded-md border border-dashed border-border text-xs text-muted-foreground">
        No charges in this period — nothing to accumulate.
      </div>
    )
  }

  const maxV = Math.max(1, ...series.map((s) => s.final))
  const y = (v: number) => H - PAD_B - (v / maxV) * (H - PAD_T - PAD_B)

  function stepPath(s: Series): string {
    let py = y(0)
    let d = `M ${x(sinceMs)} ${py}`
    for (const p of s.points) {
      const px = x(p.t)
      const ny = y(p.v)
      d += ` L ${px} ${py} L ${px} ${ny}`
      py = ny
    }
    d += ` L ${x(untilMs)} ${py}`
    return d
  }

  // End labels: sort by final value and push down to keep a minimum 13px gap
  // so two lines finishing near each other don't overwrite each other's label.
  const labelPos = [...series]
    .sort((a, b) => a.final - b.final)
    .reduce<{ s: Series; ly: number }[]>((acc, s) => {
      const target = y(s.final)
      const prev = acc[acc.length - 1]
      const ly = prev ? Math.max(target, prev.ly + 13) : target
      acc.push({ s, ly })
      return acc
    }, [])

  return (
    <div className="overflow-x-auto">
      <div className="relative min-w-[560px]">
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Cumulative charges per account">
          {Array.from({ length: 5 }, (_, i) => {
            const v = (maxV * i) / 4
            return (
              <g key={i}>
                <line x1={PAD_L} y1={y(v)} x2={W - PAD_R} y2={y(v)} className="stroke-border" strokeWidth={i === 0 ? 1 : 0.5} />
                <text x={PAD_L - 6} y={y(v) + 3} textAnchor="end" className="fill-muted-foreground" fontSize={9.5}>
                  {fmtCompact(v)}
                </text>
              </g>
            )
          })}

          {series.map((s) => (
            <path
              key={s.label + s.final}
              d={stepPath(s)}
              fill="none"
              className={s.total ? 'stroke-foreground' : undefined}
              stroke={s.total ? undefined : s.color}
              strokeWidth={s.total ? 2.5 : 1.6}
              opacity={s.total ? 0.9 : 1}
            />
          ))}

          {labelPos.map(({ s, ly }) => (
            <text
              key={`lbl-${s.label}`}
              x={W - PAD_R + 6}
              y={ly + 3}
              className={s.total ? 'fill-foreground font-medium' : 'fill-muted-foreground'}
              fontSize={s.total ? 10.5 : 9.5}
            >
              {s.label.length > 12 ? s.label.slice(0, 11) + '…' : s.label} {fmtCompact(s.final)}
            </text>
          ))}

          {/* hover read-out along the total line */}
          {series
            .find((s) => s.total)
            ?.points.map((p, i) => (
              <circle
                key={`tip-${i}`}
                cx={x(p.t)}
                cy={y(p.v)}
                r={7}
                fill="transparent"
                onMouseEnter={() => setTip({ x: x(p.t), y: y(p.v), label: 'Total charged', value: p.v })}
                onMouseLeave={() => setTip(null)}
              />
            ))}

          {Array.from({ length: 5 }, (_, i) => {
            const t = sinceMs + (span * i) / 4
            return (
              <text key={`ax-${i}`} x={x(t)} y={H - 8} textAnchor="middle" className="fill-muted-foreground" fontSize={9.5}>
                {new Date(t).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
              </text>
            )
          })}
        </svg>

        {tip && (
          <div
            className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md"
            style={{ left: `${(tip.x / W) * 100}%`, top: `${(tip.y / H) * 100}%` }}
          >
            <span className="font-medium tabular-nums">{tip.value.toLocaleString('en-US')}</span>{' '}
            <span className="text-muted-foreground">Toman — {tip.label}</span>
          </div>
        )}
      </div>
    </div>
  )
}
