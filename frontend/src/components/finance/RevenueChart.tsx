import * as React from 'react'
import { formatToman } from '@/lib/utils'

interface RevenueChartProps {
  data: { date: string; amount: number }[]
}

const WIDTH = 720
const HEIGHT = 200
const PAD_LEFT = 8
const PAD_RIGHT = 8
const PAD_TOP = 16
const PAD_BOTTOM = 24

/** Single-series (revenue) magnitude-over-time — a bar per day. One
 * sequential hue (primary), thin marks with rounded data-ends, a recessive
 * baseline, and a per-bar hover tooltip. No legend needed for one series. */
export function RevenueChart({ data }: RevenueChartProps) {
  const [hoverIndex, setHoverIndex] = React.useState<number | null>(null)

  const max = Math.max(1, ...data.map((d) => d.amount))
  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM
  const barGap = 2
  const barW = data.length > 0 ? plotW / data.length - barGap : 0

  const total = data.reduce((sum, d) => sum + d.amount, 0)

  if (total === 0) {
    return (
      <div className="flex h-[200px] items-center justify-center rounded-lg border border-dashed border-border text-sm text-muted-foreground">
        No revenue recorded in the last 30 days.
      </div>
    )
  }

  const labelEvery = Math.ceil(data.length / 6)

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full" role="img" aria-label="Revenue for the last 30 days">
        {/* recessive baseline */}
        <line
          x1={PAD_LEFT}
          y1={HEIGHT - PAD_BOTTOM}
          x2={WIDTH - PAD_RIGHT}
          y2={HEIGHT - PAD_BOTTOM}
          className="stroke-border"
          strokeWidth={1}
        />
        {data.map((d, i) => {
          const h = Math.max(2, (d.amount / max) * plotH)
          const x = PAD_LEFT + i * (barW + barGap)
          const y = HEIGHT - PAD_BOTTOM - h
          const isHover = hoverIndex === i
          return (
            <g key={d.date}>
              <rect
                x={x}
                y={y}
                width={Math.max(1, barW)}
                height={h}
                rx={Math.min(3, barW / 2)}
                className={isHover ? 'fill-primary' : 'fill-primary/70'}
                onMouseEnter={() => setHoverIndex(i)}
                onMouseLeave={() => setHoverIndex(null)}
              />
              {i % labelEvery === 0 && (
                <text
                  x={x + barW / 2}
                  y={HEIGHT - 8}
                  textAnchor="middle"
                  className="fill-muted-foreground text-[9px]"
                >
                  {d.date.slice(5)}
                </text>
              )}
            </g>
          )
        })}
      </svg>
      {hoverIndex !== null && data[hoverIndex] && (
        <div
          className="pointer-events-none absolute -translate-x-1/2 -translate-y-full rounded-md border border-border bg-popover px-2 py-1 text-xs shadow-md"
          style={{
            left: `${((PAD_LEFT + hoverIndex * (barW + barGap) + barW / 2) / WIDTH) * 100}%`,
            top: `${((HEIGHT - PAD_BOTTOM - Math.max(2, (data[hoverIndex].amount / max) * plotH)) / HEIGHT) * 100}%`,
          }}
        >
          <div className="font-medium">{formatToman(data[hoverIndex].amount)}</div>
          <div className="text-muted-foreground">{data[hoverIndex].date}</div>
        </div>
      )}
    </div>
  )
}
