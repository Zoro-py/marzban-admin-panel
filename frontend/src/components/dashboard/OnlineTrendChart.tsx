import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { reportsApi } from '@/lib/api'
import type { OnlineHistoryRange } from '@/lib/types'
import { useTheme } from '@/lib/theme'
import { cn, formatAgo } from '@/lib/utils'

const RANGES: { key: OnlineHistoryRange; label: string }[] = [
  { key: '1d', label: '1 day' },
  { key: '3d', label: '3 days' },
  { key: '1w', label: '1 week' },
  { key: '1m', label: '1 month' },
]

const WIDTH = 720
const HEIGHT = 140
const PAD_LEFT = 8
const PAD_RIGHT = 8
const PAD_TOP = 10
const PAD_BOTTOM = 20

// Reuses RevenueChart's already-validated "collected" hue (single series here,
// so no second color needed) — re-running the CVD/contrast validator for a
// new hex was unnecessary when an already-passing one fits.
const LINE = { light: '#2a78d6', dark: '#3987e5' }

/** How many currently-connected accounts, over time. Points come from the
 * regular sync job (see backend/app/sync_job.py) — there's no separate
 * poller, so a fresh install has nothing to show until sync has run a few
 * times, and the gap between points is exactly the sync interval, not
 * real-time. */
export function OnlineTrendChart() {
  const [range, setRange] = React.useState<OnlineHistoryRange>('1d')
  const [hoverIndex, setHoverIndex] = React.useState<number | null>(null)
  const { resolved } = useTheme()
  const color = LINE[resolved]

  const { data, isLoading } = useQuery({
    queryKey: ['reports', 'online-history', range],
    queryFn: () => reportsApi.onlineHistory(range),
  })

  const points = data?.points ?? []
  const max = Math.max(1, ...points.map((p) => p.online_count))
  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM

  function xFor(i: number) {
    return points.length > 1 ? PAD_LEFT + (i / (points.length - 1)) * plotW : PAD_LEFT + plotW / 2
  }
  function yFor(v: number) {
    return HEIGHT - PAD_BOTTOM - (v / max) * plotH
  }

  const linePath = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xFor(i)} ${yFor(p.online_count)}`).join(' ')
  const areaPath = points.length
    ? `${linePath} L ${xFor(points.length - 1)} ${HEIGHT - PAD_BOTTOM} L ${xFor(0)} ${HEIGHT - PAD_BOTTOM} Z`
    : ''

  const hovered = hoverIndex !== null ? points[hoverIndex] : null
  const latest = points.length ? points[points.length - 1] : null

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div>
          <h2 className="text-[13px] font-semibold">Online accounts</h2>
          {latest && (
            <p className="text-xs text-muted-foreground">
              {latest.online_count} online now · updated {formatAgo(latest.recorded_at)}
            </p>
          )}
        </div>
        <div className="flex gap-1 rounded-md bg-muted p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              onClick={() => setRange(r.key)}
              className={cn(
                'rounded px-2 py-1 text-[11px] font-medium transition-colors',
                range === r.key ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      <div className="p-4">
        {isLoading ? (
          <div className="flex h-[140px] items-center justify-center text-xs text-muted-foreground">Loading…</div>
        ) : points.length === 0 ? (
          <div className="flex h-[140px] flex-col items-center justify-center gap-1 rounded-md border border-dashed border-border text-center text-xs text-muted-foreground">
            <span>No data yet for this range.</span>
            <span>Fills in as the sync job runs — check back after a few cycles.</span>
          </div>
        ) : (
          <div className="relative">
            <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full" role="img" aria-label="Online accounts over time">
              <line x1={PAD_LEFT} y1={HEIGHT - PAD_BOTTOM} x2={WIDTH - PAD_RIGHT} y2={HEIGHT - PAD_BOTTOM} className="stroke-border" strokeWidth={1} />
              {areaPath && <path d={areaPath} fill={color} opacity={0.12} />}
              {linePath && <path d={linePath} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />}
              {points.map((p, i) => (
                <circle
                  key={p.recorded_at}
                  cx={xFor(i)}
                  cy={yFor(p.online_count)}
                  r={hoverIndex === i ? 3.5 : 0}
                  fill={color}
                  className="transition-all"
                />
              ))}
              {points.length > 0 && (
                <>
                  <text x={PAD_LEFT} y={HEIGHT - 5} textAnchor="start" className="fill-muted-foreground text-[9px]">
                    {rangeLabel(points[0].recorded_at, range)}
                  </text>
                  <text x={WIDTH - PAD_RIGHT} y={HEIGHT - 5} textAnchor="end" className="fill-muted-foreground text-[9px]">
                    {rangeLabel(points[points.length - 1].recorded_at, range)}
                  </text>
                </>
              )}
              {/* full-width hit strip so hovering anywhere near a point shows its tooltip */}
              <rect
                x={PAD_LEFT}
                y={PAD_TOP}
                width={plotW}
                height={plotH}
                fill="transparent"
                onMouseMove={(e) => {
                  const rect = (e.target as SVGRectElement).getBoundingClientRect()
                  const relX = ((e.clientX - rect.left) / rect.width) * plotW + PAD_LEFT
                  let closest = 0
                  let closestDist = Infinity
                  points.forEach((_, i) => {
                    const d = Math.abs(xFor(i) - relX)
                    if (d < closestDist) {
                      closestDist = d
                      closest = i
                    }
                  })
                  setHoverIndex(closest)
                }}
                onMouseLeave={() => setHoverIndex(null)}
              />
            </svg>

            {hovered && hoverIndex !== null && (
              <div
                className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-md border border-border bg-popover px-2.5 py-1.5 text-xs shadow-md"
                style={{
                  left: `${(xFor(hoverIndex) / WIDTH) * 100}%`,
                  top: `${(yFor(hovered.online_count) / HEIGHT) * 100}%`,
                }}
              >
                <div className="mb-0.5 text-[10px] text-muted-foreground">{formatAgo(hovered.recorded_at)}</div>
                <div className="font-medium tabular-nums">
                  {hovered.online_count} <span className="font-normal text-muted-foreground">/ {hovered.total_accounts} online</span>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

function rangeLabel(iso: string, range: OnlineHistoryRange): string {
  const d = new Date(iso.includes('Z') || /[+-]\d{2}:?\d{2}$/.test(iso) ? iso : iso.replace(' ', 'T') + 'Z')
  if (range === '1d') return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}
