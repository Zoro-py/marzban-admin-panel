import * as React from 'react'
import { useTheme } from '@/lib/theme'
import type { ChargeHistory } from '@/lib/types'
import { cn, formatToman, parseDate } from '@/lib/utils'
import { formatJalali } from '@/lib/jalali'
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

/** The step line's value at an arbitrary time t: the running total as of the
 * LAST real charge at or before t (0 before the first charge). Used to
 * follow the mouse continuously instead of only snapping to charge events. */
function valueAtTime(s: Series, t: number): number {
  let v = 0
  for (const p of s.points) {
    if (p.t > t) break
    v = p.v
  }
  return v
}

export function CumulativeChart({ data, sinceMs, untilMs }: { data: ChargeHistory; sinceMs: number; untilMs: number }) {
  const { resolved } = useTheme()
  const [hoverT, setHoverT] = React.useState<number | null>(null)
  const svgRef = React.useRef<SVGSVGElement>(null)

  const span = Math.max(1, untilMs - sinceMs)
  const x = (t: number) => PAD_L + ((t - sinceMs) / span) * (W - PAD_L - PAD_R)

  const series = React.useMemo<Series[]>(() => {
    const out: Series[] = []
    data.accounts.forEach((a, i) => {
      const charges = data.entries
        .filter((e) => e.account_id === a.id && e.type === 'charge')
        .filter((e) => { const t = parseDate(e.date).getTime(); return t >= sinceMs && t <= untilMs })
        .sort((p, q) => p.date.localeCompare(q.date))
      if (charges.length === 0) return
      let cum = 0
      const points = charges.map((e) => ({ t: parseDate(e.date).getTime(), v: (cum += e.amount) }))
      out.push({ label: a.username, color: accountColor(resolved, i), points, final: cum })
    })
    const all = [...data.entries]
      .filter((e) => e.type === 'charge')
      .filter((e) => { const t = parseDate(e.date).getTime(); return t >= sinceMs && t <= untilMs })
      .sort((p, q) => p.date.localeCompare(q.date))
    if (all.length > 0) {
      let cum = 0
      const points = all.map((e) => ({ t: parseDate(e.date).getTime(), v: (cum += e.amount) }))
      out.push({ label: 'Total', color: 'foreground', points, final: cum, total: true })
    }
    return out
  }, [data, resolved, sinceMs, untilMs])

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
    .sort((a, b) => b.final - a.final)
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
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="w-full"
          role="img"
          aria-label="Cumulative charges per account — hover or move your finger along the plot to read every account's running total at that moment"
          onMouseMove={(evt) => {
            const svg = svgRef.current
            if (!svg) return
            const pt = svg.createSVGPoint()
            pt.x = evt.clientX
            pt.y = evt.clientY
            const ctm = svg.getScreenCTM()
            if (!ctm) return
            const local = pt.matrixTransform(ctm.inverse())
            const plotL = PAD_L
            const plotR = W - PAD_R
            if (local.x < plotL || local.x > plotR) {
              setHoverT(null)
              return
            }
            const t = sinceMs + ((local.x - plotL) / (plotR - plotL)) * span
            setHoverT(Math.min(untilMs, Math.max(sinceMs, t)))
          }}
          onMouseLeave={() => setHoverT(null)}
          onTouchMove={(evt) => {
            const svg = svgRef.current
            const touch = evt.touches[0]
            if (!svg || !touch) return
            const pt = svg.createSVGPoint()
            pt.x = touch.clientX
            pt.y = touch.clientY
            const ctm = svg.getScreenCTM()
            if (!ctm) return
            const local = pt.matrixTransform(ctm.inverse())
            const plotL = PAD_L
            const plotR = W - PAD_R
            if (local.x < plotL || local.x > plotR) return
            const t = sinceMs + ((local.x - plotL) / (plotR - plotL)) * span
            setHoverT(Math.min(untilMs, Math.max(sinceMs, t)))
          }}
          onTouchEnd={() => setHoverT(null)}
        >
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

          {/* Crosshair: follows the mouse/finger continuously (not just at charge
              events) and shows every series' running total at that instant —
              the "trace the trend" reading the chart alone doesn't give. */}
          {hoverT != null && (
            <>
              <line
                x1={x(hoverT)}
                y1={PAD_T}
                x2={x(hoverT)}
                y2={H - PAD_B}
                className="stroke-muted-foreground"
                strokeWidth={1}
                strokeDasharray="3 3"
                pointerEvents="none"
              />
              {series.map((s) => (
                <circle
                  key={`cross-${s.label}`}
                  cx={x(hoverT)}
                  cy={y(valueAtTime(s, hoverT))}
                  r={s.total ? 3.5 : 3}
                  className={cn(s.total ? 'fill-foreground' : undefined, 'stroke-card')}
                  fill={s.total ? undefined : s.color}
                  strokeWidth={1.5}
                  pointerEvents="none"
                />
              ))}
            </>
          )}

          {/* Invisible full-height strip over the plot area — this, not the
              individual series paths, is what actually receives the pointer
              events above (a 1.6px step line is too thin a target). */}
          <rect x={PAD_L} y={PAD_T} width={W - PAD_L - PAD_R} height={H - PAD_T - PAD_B} fill="transparent" />

          {Array.from({ length: 5 }, (_, i) => {
            const t = sinceMs + (span * i) / 4
            const d = new Date(t)
            return (
              <g key={`ax-${i}`}>
                <text x={x(t)} y={H - 18} textAnchor="middle" className="fill-muted-foreground" fontSize={9.5}>
                  {d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                </text>
                <text x={x(t)} y={H - 7} textAnchor="middle" className="fill-muted-foreground/70" fontSize={8.5}>
                  {formatJalali(d)}
                </text>
              </g>
            )
          })}
        </svg>

        {hoverT != null && (() => {
          const d = new Date(hoverT)
          const rows = [...series].sort((a, b) => valueAtTime(b, hoverT) - valueAtTime(a, hoverT))
          const crossX = x(hoverT)
          // Flip to the left of the crosshair once it's past ~70% of the plot
          // width, so the box never runs off the right edge under the labels.
          const flip = crossX > PAD_L + (W - PAD_L - PAD_R) * 0.7
          return (
            <div
              className="pointer-events-none absolute z-10 min-w-[150px] -translate-y-1/2 rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md"
              style={{
                left: `${(crossX / W) * 100}%`,
                top: '50%',
                transform: `translateY(-50%) translateX(${flip ? 'calc(-100% - 10px)' : '10px'})`,
              }}
            >
              <p className="mb-1 whitespace-nowrap font-medium">
                {d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} · {formatJalali(d)}
              </p>
              {rows.map((s) => (
                <p key={s.label} className="flex items-center gap-1.5 whitespace-nowrap">
                  <span
                    className={cn('inline-block h-2 w-2 shrink-0 rounded-full', s.total && 'bg-foreground')}
                    style={s.total ? undefined : { background: s.color }}
                  />
                  <span className={cn('truncate', s.total ? 'font-medium' : 'text-muted-foreground')}>
                    {s.label}
                  </span>
                  <span className="ml-auto tabular-nums">{formatToman(valueAtTime(s, hoverT))}</span>
                </p>
              ))}
            </div>
          )
        })()}
      </div>
    </div>
  )
}
