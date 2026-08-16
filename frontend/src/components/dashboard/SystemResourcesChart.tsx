import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { reportsApi } from '@/lib/api'
import { useTheme } from '@/lib/theme'

const POLL_MS = 3000
// A live rolling window, not a history — "watch it while the panel's open"
// needs the last few minutes, not an ever-growing buffer. The persisted,
// multi-day trend already exists as the separate "Online accounts" chart.
const MAX_SAMPLES = 80 // 4 minutes at POLL_MS=3000

const WIDTH = 720
const HEIGHT = 120
const PAD_LEFT = 8
const PAD_RIGHT = 8
const PAD_TOP = 8
const PAD_BOTTOM = 16

// Reuses RevenueChart's already-validated categorical pair (CVD ΔE 73.6/69.8
// light/dark) — CPU takes the "collected" blue slot, RAM the "charged"
// green slot, same fixed order rule as any other categorical pairing here.
const SERIES = {
  light: { cpu: '#2a78d6', mem: '#1baf7a' },
  dark: { cpu: '#3987e5', mem: '#199e70' },
}

function agoLabel(sinceMs: number): string {
  const totalSec = Math.max(0, Math.round((Date.now() - sinceMs) / 1000))
  return totalSec < 60 ? `-${totalSec}s` : `-${Math.round(totalSec / 60)}m`
}

interface Sample {
  t: number // client Date.now() at receipt — the endpoint itself is a stateless snapshot with no timestamp of its own
  cpu: number
  mem: number
  memUsedMb: number
  memTotalMb: number
  load1: number | null
}

/** Live CPU/RAM while the dashboard is open — polls a stateless host snapshot
 * every POLL_MS and keeps its own short rolling buffer client-side (nothing
 * persisted server-side; see backend/app/routers/reports.py:system_status).
 * Polling pauses automatically when the tab isn't focused (React Query's
 * refetchIntervalInBackground defaults to false) — exactly "while the panel's
 * open" with no extra wiring. */
export function SystemResourcesChart() {
  const [samples, setSamples] = React.useState<Sample[]>([])
  const [hoverIndex, setHoverIndex] = React.useState<number | null>(null)
  const { resolved } = useTheme()
  const colors = SERIES[resolved]

  const { data, isError } = useQuery({
    queryKey: ['reports', 'system-status'],
    queryFn: reportsApi.systemStatus,
    refetchInterval: POLL_MS,
  })

  React.useEffect(() => {
    if (!data) return
    setSamples((prev) => [
      ...prev,
      { t: Date.now(), cpu: data.cpu_percent, mem: data.mem_percent, memUsedMb: data.mem_used_mb, memTotalMb: data.mem_total_mb, load1: data.load_avg_1m },
    ].slice(-MAX_SAMPLES))
  }, [data])

  const plotW = WIDTH - PAD_LEFT - PAD_RIGHT
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM

  function xFor(i: number) {
    return samples.length > 1 ? PAD_LEFT + (i / (samples.length - 1)) * plotW : PAD_LEFT + plotW / 2
  }
  // Fixed 0-100 scale — both series are percentages, so one axis serves both
  // without normalizing anything.
  function yFor(pct: number) {
    return HEIGHT - PAD_BOTTOM - (Math.min(100, Math.max(0, pct)) / 100) * plotH
  }

  function pathFor(key: 'cpu' | 'mem') {
    return samples.map((s, i) => `${i === 0 ? 'M' : 'L'} ${xFor(i)} ${yFor(s[key])}`).join(' ')
  }

  const latest = samples.length ? samples[samples.length - 1] : null
  const hovered = hoverIndex !== null ? samples[hoverIndex] : null

  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <div>
          <h2 className="text-[13px] font-semibold">Server resources</h2>
          {latest && (
            <p className="text-xs text-muted-foreground">
              CPU {latest.cpu.toFixed(0)}% · RAM {latest.mem.toFixed(0)}% ({latest.memUsedMb.toLocaleString('en-US')} / {latest.memTotalMb.toLocaleString('en-US')} MB)
              {latest.load1 !== null && <> · load {latest.load1.toFixed(2)}</>}
            </p>
          )}
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <span className="h-0.5 w-2.5 rounded" style={{ background: colors.cpu }} />
            CPU
          </span>
          <span className="flex items-center gap-1.5">
            <span className="h-0.5 w-2.5 rounded" style={{ background: colors.mem }} />
            RAM
          </span>
        </div>
      </div>

      <div className="p-4">
        {isError ? (
          <div className="flex h-[120px] items-center justify-center text-xs text-muted-foreground">
            Couldn't load live resource stats.
          </div>
        ) : samples.length === 0 ? (
          <div className="flex h-[120px] items-center justify-center text-xs text-muted-foreground">Loading…</div>
        ) : (
          <div className="relative">
            <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full" role="img" aria-label="Live CPU and RAM usage">
              {/* recessive 50% reference line — the one gridline worth having on a 0-100% scale */}
              <line x1={PAD_LEFT} y1={yFor(50)} x2={WIDTH - PAD_RIGHT} y2={yFor(50)} className="stroke-border" strokeWidth={1} opacity={0.5} />
              <line x1={PAD_LEFT} y1={HEIGHT - PAD_BOTTOM} x2={WIDTH - PAD_RIGHT} y2={HEIGHT - PAD_BOTTOM} className="stroke-border" strokeWidth={1} />
              <path d={pathFor('mem')} fill="none" stroke={colors.mem} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              <path d={pathFor('cpu')} fill="none" stroke={colors.cpu} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
              {hoverIndex !== null && (
                <line x1={xFor(hoverIndex)} y1={PAD_TOP} x2={xFor(hoverIndex)} y2={HEIGHT - PAD_BOTTOM} className="stroke-border" strokeWidth={1} />
              )}
              <text x={PAD_LEFT} y={HEIGHT - 4} textAnchor="start" className="fill-muted-foreground text-[9px]">
                {agoLabel(samples[0].t)}
              </text>
              <text x={WIDTH - PAD_RIGHT} y={HEIGHT - 4} textAnchor="end" className="fill-muted-foreground text-[9px]">
                now
              </text>
              {/* full-width hit strip, same crosshair pattern as OnlineTrendChart */}
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
                  samples.forEach((_, i) => {
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
                  top: `${(Math.min(yFor(hovered.cpu), yFor(hovered.mem)) / HEIGHT) * 100}%`,
                }}
              >
                <div className="mb-0.5 text-[10px] text-muted-foreground">{Math.round((Date.now() - hovered.t) / 1000)}s ago</div>
                <div className="flex items-center gap-1.5 whitespace-nowrap">
                  <span className="h-0.5 w-2.5 rounded" style={{ background: colors.cpu }} />
                  <span className="font-medium tabular-nums">{hovered.cpu.toFixed(0)}%</span>
                  <span className="text-muted-foreground">CPU</span>
                </div>
                <div className="flex items-center gap-1.5 whitespace-nowrap">
                  <span className="h-0.5 w-2.5 rounded" style={{ background: colors.mem }} />
                  <span className="font-medium tabular-nums">{hovered.mem.toFixed(0)}%</span>
                  <span className="text-muted-foreground">RAM ({hovered.memUsedMb.toLocaleString('en-US')} MB)</span>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
