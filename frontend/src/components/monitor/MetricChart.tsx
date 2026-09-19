import * as React from 'react'
import { useTheme } from '@/lib/theme'

interface Series {
  label: string
  values: (number | null)[]
  color: { light: string; dark: string }
  dashed?: boolean
}

interface MetricChartProps {
  /** One shared value axis (same unit) — mirror of RevenueChart's rule:
   * pair series that answer one question (cpu with steal, rx with tx). */
  series: Series[]
  /** Value→label for the hover tooltip and y-max caption. */
  format: (v: number) => string
  /** Sample timestamps, parallel to series values. */
  times: string[]
  height?: number
  /** Fixed y-max (e.g. 100 for percents) so spikes don't rescale history. */
  yMax?: number
}

const WIDTH = 720
const PAD_LEFT = 8
const PAD_RIGHT = 8
const PAD_TOP = 10
const PAD_BOTTOM = 20

/* Slots 1+2 of the validated dataviz palette (same as RevenueChart): series
 * identity, not state — severity colors stay reserved for status meaning. */
export const SERIES_BLUE = { light: '#2a78d6', dark: '#3987e5' }
export const SERIES_AQUA = { light: '#1baf7a', dark: '#199e70' }

/** Multi-series line chart for the server history views. Bespoke SVG like
 * RevenueChart — no chart library in the bundle for three small graphs. */
export function MetricChart({ series, format, times, height = 180, yMax }: MetricChartProps) {
  const [hoverIndex, setHoverIndex] = React.useState<number | null>(null)
  const { resolved } = useTheme()

  const HEIGHT = height
  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM

  const numeric = series.flatMap((s) => s.values.filter((v): v is number => v != null))
  const max = yMax ?? Math.max(1, ...numeric)
  const n = times.length

  if (n === 0 || numeric.length === 0) {
    return (
      <div className="flex h-[140px] items-center justify-center rounded-md border border-dashed border-border text-xs text-muted-foreground">
        No samples yet.
      </div>
    )
  }

  const xFor = (i: number) => PAD_LEFT + (n <= 1 ? 0 : (i / (n - 1)) * plotW)
  const yFor = (v: number) => HEIGHT - PAD_BOTTOM - Math.max(0, Math.min(1, v / max)) * plotH

  const labelEvery = Math.max(1, Math.ceil(n / 6))
  const hovered = hoverIndex !== null ? times[hoverIndex] : null

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-4 text-[11px] text-muted-foreground">
        {series.map((s) => (
          <span key={s.label} className="flex items-center gap-1.5">
            <span
              className="h-0.5 w-3 rounded"
              style={{ background: s.color[resolved], opacity: s.dashed ? 0.7 : 1 }}
            />
            {s.label}
          </span>
        ))}
        <span className="ml-auto tabular-nums">max {format(max)}</span>
      </div>

      <div className="relative">
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full" role="img" aria-label={series.map((s) => s.label).join(' and ')}>
          <line x1={PAD_LEFT} y1={HEIGHT - PAD_BOTTOM} x2={WIDTH - PAD_RIGHT} y2={HEIGHT - PAD_BOTTOM} className="stroke-border" strokeWidth={1} />
          {times.map((t, i) =>
            i % labelEvery === 0 ? (
              <text key={t + i} x={xFor(i)} y={HEIGHT - 6} textAnchor="middle" className="fill-muted-foreground text-[9px]">
                {t.slice(11, 16)}
              </text>
            ) : null,
          )}
          {series.map((s) => {
            const pts = s.values
              .map((v, i) => (v == null ? null : `${xFor(i)},${yFor(v)}`))
              .filter((p): p is string => p != null)
            if (pts.length === 0) return null
            return (
              <polyline
                key={s.label}
                points={pts.join(' ')}
                fill="none"
                stroke={s.color[resolved]}
                strokeWidth={1.5}
                strokeDasharray={s.dashed ? '4 3' : undefined}
                strokeLinejoin="round"
              />
            )
          })}
          {hoverIndex !== null && (
            <line x1={xFor(hoverIndex)} y1={PAD_TOP} x2={xFor(hoverIndex)} y2={HEIGHT - PAD_BOTTOM} className="stroke-border" strokeWidth={1} />
          )}
          {times.map((t, i) => (
            <rect
              key={'hit' + t + i}
              x={xFor(i) - plotW / (2 * Math.max(1, n - 1))}
              y={PAD_TOP}
              width={plotW / Math.max(1, n - 1)}
              height={plotH}
              fill="transparent"
              onMouseEnter={() => setHoverIndex(i)}
              onMouseLeave={() => setHoverIndex(null)}
            />
          ))}
        </svg>

        {hovered && hoverIndex !== null && (
          <div
            className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md"
            style={{ left: `${(xFor(hoverIndex) / WIDTH) * 100}%`, top: `${(PAD_TOP / HEIGHT) * 100}%` }}
          >
            <div className="mb-1 text-[10px] text-muted-foreground">{hovered.slice(5, 16).replace('T', ' ')}</div>
            {series.map((s) => (
              <div key={s.label} className="flex items-center gap-1.5 whitespace-nowrap">
                <span className="h-0.5 w-2.5 rounded" style={{ background: s.color[resolved] }} />
                <span className="font-medium tabular-nums">
                  {s.values[hoverIndex] == null ? '—' : format(s.values[hoverIndex] as number)}
                </span>
                <span className="text-muted-foreground">{s.label}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/** Thumb-sized single-series sparkline for the server cards — a glance
 * trend, not a chart you read numbers off (click through for that). */
export function Sparkline({ values, className }: { values: (number | null)[]; className?: string }) {
  const { resolved } = useTheme()
  const w = 120
  const h = 28
  const numeric = values.filter((v): v is number => v != null)
  if (numeric.length < 2) {
    return <div className={`h-[28px] w-[120px] ${className ?? ''}`} />
  }
  const max = Math.max(1, ...numeric)
  const pts = values
    .map((v, i) =>
      v == null
        ? null
        : `${(i / (values.length - 1)) * w},${h - Math.max(0, Math.min(1, v / max)) * (h - 2) - 1}`,
    )
    .filter((p): p is string => p != null)
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className={`w-[120px] ${className ?? ''}`} aria-hidden>
      <polyline points={pts.join(' ')} fill="none" stroke={SERIES_BLUE[resolved]} strokeWidth={1.25} strokeLinejoin="round" />
    </svg>
  )
}
