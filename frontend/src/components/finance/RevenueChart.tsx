import * as React from 'react'
import { useTheme } from '@/lib/theme'
import { formatToman } from '@/lib/utils'

interface RevenueChartProps {
  collected: { date: string; amount: number }[]
  charged: { date: string; amount: number }[]
}

const WIDTH = 720
const HEIGHT = 220
const PAD_LEFT = 8
const PAD_RIGHT = 8
const PAD_TOP = 12
const PAD_BOTTOM = 22

/* Categorical slots 1+2 of the validated reference palette, stepped per
 * surface (light/dark validated separately — CVD ΔE 73.6/69.8, both pass).
 * Light-mode aqua is < 3:1 vs the surface, so the relief rule applies: the
 * legend, the per-day tooltip, and the transactions table below the chart
 * all carry the values without relying on the fill color. */
const SERIES = {
  light: { collected: '#2a78d6', charged: '#1baf7a' },
  dark: { collected: '#3987e5', charged: '#199e70' },
}

/** Charged vs collected, per day, last 30 days — two thin bars per day from
 * one baseline (same unit, one axis). "Billed a lot this week but nothing
 * came in yet" is the question this pairing answers. */
export function RevenueChart({ collected, charged }: RevenueChartProps) {
  const [hoverIndex, setHoverIndex] = React.useState<number | null>(null)
  const { resolved } = useTheme()
  const colors = SERIES[resolved]

  const chargedByDate = React.useMemo(() => new Map(charged.map((d) => [d.date, d.amount])), [charged])
  const days = React.useMemo(
    () => collected.map((d) => ({ date: d.date, collected: d.amount, charged: chargedByDate.get(d.date) ?? 0 })),
    [collected, chargedByDate],
  )

  const max = Math.max(1, ...days.map((d) => Math.max(d.collected, d.charged)))
  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM
  const band = days.length > 0 ? plotW / days.length : 0
  const gap = 2 // surface gap between the pair and between neighbors
  const barW = Math.max(2, Math.min(9, (band - 3 * gap) / 2))

  const total = days.reduce((sum, d) => sum + d.collected + d.charged, 0)
  if (total === 0) {
    return (
      <div className="flex h-[220px] items-center justify-center rounded-md border border-dashed border-border text-xs text-muted-foreground">
        No money recorded in the last 30 days.
      </div>
    )
  }

  const labelEvery = Math.ceil(days.length / 6)
  const hovered = hoverIndex !== null ? days[hoverIndex] : null

  function yFor(v: number) {
    return HEIGHT - PAD_BOTTOM - Math.max(v > 0 ? 2 : 0, (v / max) * plotH)
  }

  return (
    <div className="flex flex-col gap-2">
      {/* Legend — always present for two series; text wears text tokens,
          the swatch carries identity. */}
      <div className="flex items-center gap-4 text-[11px] text-muted-foreground">
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-[2px]" style={{ background: colors.charged }} />
          Charged (billed)
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-[2px]" style={{ background: colors.collected }} />
          Collected (payments in)
        </span>
      </div>

      <div className="relative">
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full" role="img" aria-label="Charged vs collected, last 30 days">
          {/* recessive baseline */}
          <line
            x1={PAD_LEFT}
            y1={HEIGHT - PAD_BOTTOM}
            x2={WIDTH - PAD_RIGHT}
            y2={HEIGHT - PAD_BOTTOM}
            className="stroke-border"
            strokeWidth={1}
          />
          {days.map((d, i) => {
            const x0 = PAD_LEFT + i * band
            const pairW = 2 * barW + gap
            const xCharged = x0 + (band - pairW) / 2
            const xCollected = xCharged + barW + gap
            const isHover = hoverIndex === i
            return (
              <g key={d.date} opacity={hoverIndex === null || isHover ? 1 : 0.55}>
                {d.charged > 0 && (
                  <path d={roundedTopBar(xCharged, yFor(d.charged), barW, HEIGHT - PAD_BOTTOM - yFor(d.charged))} fill={colors.charged} />
                )}
                {d.collected > 0 && (
                  <path d={roundedTopBar(xCollected, yFor(d.collected), barW, HEIGHT - PAD_BOTTOM - yFor(d.collected))} fill={colors.collected} />
                )}
                {i % labelEvery === 0 && (
                  <text x={x0 + band / 2} y={HEIGHT - 7} textAnchor="middle" className="fill-muted-foreground text-[9px]">
                    {d.date.slice(5)}
                  </text>
                )}
                {/* full-band hit target: readers aim at a day, not a 6px bar */}
                <rect
                  x={x0}
                  y={PAD_TOP}
                  width={band}
                  height={plotH}
                  fill="transparent"
                  onMouseEnter={() => setHoverIndex(i)}
                  onMouseLeave={() => setHoverIndex(null)}
                />
              </g>
            )
          })}
        </svg>

        {hovered && hoverIndex !== null && (
          <div
            className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md"
            style={{
              left: `${((PAD_LEFT + hoverIndex * band + band / 2) / WIDTH) * 100}%`,
              top: `${(Math.min(yFor(hovered.collected), yFor(hovered.charged)) / HEIGHT) * 100}%`,
            }}
          >
            <div className="mb-1 text-[10px] text-muted-foreground">{hovered.date}</div>
            <div className="flex items-center gap-1.5 whitespace-nowrap">
              <span className="h-0.5 w-2.5 rounded" style={{ background: colors.charged }} />
              <span className="font-medium tabular-nums">{formatToman(hovered.charged)}</span>
              <span className="text-muted-foreground">charged</span>
            </div>
            <div className="flex items-center gap-1.5 whitespace-nowrap">
              <span className="h-0.5 w-2.5 rounded" style={{ background: colors.collected }} />
              <span className="font-medium tabular-nums">{formatToman(hovered.collected)}</span>
              <span className="text-muted-foreground">collected</span>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

/** Bar with a 3px rounded data-end and a square baseline end. */
function roundedTopBar(x: number, y: number, w: number, h: number): string {
  const r = Math.min(3, w / 2, h)
  const bottom = y + h
  return [
    `M ${x} ${bottom}`,
    `L ${x} ${y + r}`,
    `Q ${x} ${y} ${x + r} ${y}`,
    `L ${x + w - r} ${y}`,
    `Q ${x + w} ${y} ${x + w} ${y + r}`,
    `L ${x + w} ${bottom}`,
    'Z',
  ].join(' ')
}
