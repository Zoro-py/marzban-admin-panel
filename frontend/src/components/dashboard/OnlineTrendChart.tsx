import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { Moon } from 'lucide-react'
import { reportsApi } from '@/lib/api'
import type { OnlineHistoryPoint, OnlineHistoryRange } from '@/lib/types'
import { useTheme } from '@/lib/theme'
import { cn, formatAgo, parseDate } from '@/lib/utils'

const RANGES: { key: OnlineHistoryRange; label: string; days: number }[] = [
  { key: '1d', label: '1 day', days: 1 },
  { key: '3d', label: '3 days', days: 3 },
  { key: '1w', label: '1 week', days: 7 },
  { key: '1m', label: '1 month', days: 30 },
]

const WIDTH = 720
const HEIGHT = 140
const PAD_LEFT = 8
const PAD_RIGHT = 8
const PAD_TOP = 10
const PAD_BOTTOM = 20
const TICK_COUNT = 6
// A maintenance window shorter than this isn't really actionable; longer
// starts eating into hours that aren't actually quiet. 3h matches a
// realistic "upgrade + reboot + watch it come back" slot.
const QUIET_WINDOW_HOURS = 3
// Below this many distinct hour-of-day buckets, "quietest window" would be
// guessing from a handful of points rather than reading a real daily
// pattern — better to say nothing than to report a false pattern.
const MIN_HOURS_FOR_INSIGHT = 6

// Reuses RevenueChart's already-validated "collected" hue (single series here,
// so no second color needed) — re-running the CVD/contrast validator for a
// new hex was unnecessary when an already-passing one fits.
const LINE = { light: '#2a78d6', dark: '#3987e5' }

interface QuietWindow {
  startHour: number
  endHour: number
  avgOnline: number
  hoursObserved: number
}

/** Average online_count per local hour-of-day across every loaded point, then
 * find the QUIET_WINDOW_HOURS-long circular window (23→0 wraps) with the
 * lowest average — the recurring daily lull, not just today's local dip.
 * More days in the loaded range means more samples per hour bucket, so the
 * result gets more reliable (not just less noisy) as the range widens. */
function findQuietWindow(points: OnlineHistoryPoint[]): QuietWindow | null {
  const buckets: number[][] = Array.from({ length: 24 }, () => [])
  for (const p of points) {
    buckets[parseDate(p.recorded_at).getHours()].push(p.online_count)
  }
  const hourAvg = buckets.map((b) => (b.length ? b.reduce((s, v) => s + v, 0) / b.length : null))
  const hoursObserved = hourAvg.filter((v) => v !== null).length
  if (hoursObserved < MIN_HOURS_FOR_INSIGHT) return null

  let best: { start: number; avg: number } | null = null
  for (let start = 0; start < 24; start++) {
    const window = Array.from({ length: QUIET_WINDOW_HOURS }, (_, i) => hourAvg[(start + i) % 24])
    if (window.some((v) => v === null)) continue
    const avg = (window as number[]).reduce((s, v) => s + v, 0) / QUIET_WINDOW_HOURS
    if (!best || avg < best.avg) best = { start, avg }
  }
  if (!best) return null
  return {
    startHour: best.start,
    endHour: (best.start + QUIET_WINDOW_HOURS) % 24,
    avgOnline: best.avg,
    hoursObserved,
  }
}

function isHourInWindow(hour: number, start: number, end: number): boolean {
  return start < end ? hour >= start && hour < end : hour >= start || hour < end
}

function formatHour(h: number): string {
  return `${String(h).padStart(2, '0')}:00`
}

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
  const rangeDays = RANGES.find((r) => r.key === range)?.days ?? 1

  const quiet = React.useMemo(() => findQuietWindow(points), [points])

  // Every contiguous run of points whose hour falls in the quiet window —
  // recurs once per day in the loaded range, not just once on the chart.
  // Skipped for 1-month view: at that zoom each day's sliver is a couple
  // px wide and thirty of them reads as noise, not signal.
  const quietBands = React.useMemo(() => {
    if (!quiet || range === '1m' || points.length < 2) return []
    const bands: { x1: number; x2: number }[] = []
    let segStart: number | null = null
    points.forEach((p, i) => {
      const inWindow = isHourInWindow(parseDate(p.recorded_at).getHours(), quiet.startHour, quiet.endHour)
      if (inWindow && segStart === null) segStart = i
      if (!inWindow && segStart !== null) {
        bands.push({ x1: xFor(segStart), x2: xFor(i - 1) })
        segStart = null
      }
    })
    if (segStart !== null) bands.push({ x1: xFor(segStart), x2: xFor(points.length - 1) })
    return bands
  }, [points, quiet, range])

  const tickIndices = React.useMemo(() => {
    if (points.length === 0) return []
    if (points.length <= TICK_COUNT) return points.map((_, i) => i)
    const idx = Array.from({ length: TICK_COUNT }, (_, k) => Math.round((k / (TICK_COUNT - 1)) * (points.length - 1)))
    return idx.filter((v, i, arr) => arr.indexOf(v) === i)
  }, [points])

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

      {quiet && (
        <div className="flex items-center gap-1.5 border-b border-border bg-muted/30 px-4 py-1.5 text-xs text-muted-foreground">
          <Moon className="h-3 w-3 shrink-0" />
          <span>
            Quietest window: <span className="font-medium text-foreground">{formatHour(quiet.startHour)}–{formatHour(quiet.endHour)}</span>
            {' '}· avg {Math.round(quiet.avgOnline)} online · last {rangeDays}d, {quiet.hoursObserved}/24h observed
          </span>
        </div>
      )}

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
              {tickIndices.map((i) => (
                <line
                  key={`grid-${i}`}
                  x1={xFor(i)}
                  y1={PAD_TOP}
                  x2={xFor(i)}
                  y2={HEIGHT - PAD_BOTTOM}
                  className="stroke-border"
                  strokeWidth={1}
                  opacity={0.5}
                />
              ))}
              {quietBands.map((b, i) => (
                <rect
                  key={`quiet-${i}`}
                  x={b.x1}
                  y={PAD_TOP}
                  width={Math.max(1, b.x2 - b.x1)}
                  height={plotH}
                  className="fill-muted-foreground"
                  opacity={0.08}
                />
              ))}
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
              {tickIndices.map((i, k) => (
                <text
                  key={`tick-${i}`}
                  x={xFor(i)}
                  y={HEIGHT - 5}
                  textAnchor={k === 0 ? 'start' : k === tickIndices.length - 1 ? 'end' : 'middle'}
                  className="fill-muted-foreground text-[9px]"
                >
                  {tickLabel(points[i].recorded_at, range)}
                </text>
              ))}
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

function tickLabel(iso: string, range: OnlineHistoryRange): string {
  const d = parseDate(iso)
  if (range === '1d') return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
  if (range === '3d' || range === '1w') {
    return `${d.toLocaleDateString('en-US', { weekday: 'short' })} ${d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })}`
  }
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}
