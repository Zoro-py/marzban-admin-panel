import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { monitorApi } from '@/lib/api'
import type { ServerMetricPoint } from '@/lib/types'
import { MetricChart, SERIES_AQUA, SERIES_BLUE } from '@/components/monitor/MetricChart'
import { cn } from '@/lib/utils'

/** One live-monitorable scalar per agent sample (mirrors ServerMetric — the
 * derived ones like mem% are computed here so the DB stays denormalized-free). */
interface FieldDef {
  key: string
  label: string
  unit: string
  format: (v: number) => string
  max?: number
  /** Second line on the same axis, same unit — only where the pairing
   * answers one question (cpu with steal, rx with tx). */
  paired?: { key: string; label: string }
  /** Optional extra key from the same point family shown as context text. */
  severity?: (v: number) => 'ok' | 'warn' | 'crit'
}

const FIELDS: FieldDef[] = [
  {
    key: 'cpu_pct', label: 'CPU', unit: '%', format: (v) => `${v.toFixed(1)}%`, max: 100,
    paired: { key: 'steal_pct', label: 'steal' },
    severity: (v) => (v >= 90 ? 'crit' : v >= 75 ? 'warn' : 'ok'),
  },
  {
    key: 'steal_pct', label: 'CPU steal', unit: '%', format: (v) => `${v.toFixed(1)}%`, max: 100,
    severity: (v) => (v >= 10 ? 'crit' : v >= 5 ? 'warn' : 'ok'),
  },
  {
    key: 'load1', label: 'Load (1m)', unit: '', format: (v) => v.toFixed(2),
    paired: { key: 'load5', label: 'load5' },
  },
  {
    key: 'mem_used_mb', label: 'Memory used', unit: 'MB', format: (v) => `${(v / 1024).toFixed(2)} GB`,
  },
  {
    key: 'mem_avail_mb', label: 'Memory available', unit: 'MB', format: (v) => `${(v / 1024).toFixed(2)} GB`,
    severity: (v) => (v < 100 ? 'crit' : v < 250 ? 'warn' : 'ok'),
  },
  { key: 'swap_used_mb', label: 'Swap used', unit: 'MB', format: (v) => `${(v / 1024).toFixed(2)} GB` },
  {
    key: 'disk_used_pct', label: 'Disk used', unit: '%', format: (v) => `${v.toFixed(1)}%`, max: 100,
    severity: (v) => (v >= 90 ? 'crit' : v >= 80 ? 'warn' : 'ok'),
  },
  {
    key: 'net_rx_bps', label: 'Network in (rx)', unit: 'bps', format: (v) => formatBps(v),
    paired: { key: 'net_tx_bps', label: 'out (tx)' },
  },
  {
    key: 'tcp_retrans_pct', label: 'TCP retransmit', unit: '%', format: (v) => `${v.toFixed(2)}%`,
    severity: (v) => (v >= 5 ? 'crit' : v >= 2 ? 'warn' : 'ok'),
  },
  {
    key: 'conntrack_count', label: 'Conntrack entries', unit: '', format: (v) => v.toLocaleString('en-US'),
  },
  { key: 'net_err_delta', label: 'NIC errors /min', unit: '', format: (v) => v.toFixed(0) },
  { key: 'net_drop_delta', label: 'NIC drops /min', unit: '', format: (v) => v.toFixed(0) },
]

function formatBps(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)} Gbps`
  if (v >= 1e6) return `${(v / 1e6).toFixed(2)} Mbps`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)} Kbps`
  return `${v.toFixed(0)} bps`
}

const SEVERITY_TEXT = { ok: 'text-success', warn: 'text-warning', crit: 'text-destructive' } as const

/** Live single-field monitor: any metric the agent samples, on a 10s poll
 * with a live 30-minute window. The field picker makes "کم‌کم گذاشتن" the
 * default — new fields need zero new UI. */
export function LiveMetricMonitor({ serverId, points }: { serverId: string; points: ServerMetricPoint[] }) {
  const [fieldKey, setFieldKey] = React.useState('cpu_pct')
  const field = FIELDS.find((f) => f.key === fieldKey) ?? FIELDS[0]

  // Live polling query on top of the shared history: refetches every 10s and
  // only keeps the last 30 minutes on screen — the "what is it doing RIGHT
  // NOW" view next to the 6h/24h/7d charts.
  const liveQuery = useQuery({
    queryKey: ['monitor', 'live', serverId, fieldKey],
    queryFn: () => monitorApi.history(serverId, 1),
    refetchInterval: 10_000,
    refetchIntervalInBackground: false,
  })
  const live = liveQuery.data ?? points
  const windowStart = Date.now() - 30 * 60 * 1000
  const windowed = React.useMemo(
    () =>
      live.filter((p) => {
        const t = new Date(p.ts.endsWith('Z') ? p.ts : p.ts + 'Z').getTime()
        return t >= windowStart
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [live],
  )
  const latest = windowed.length > 0 ? windowed[windowed.length - 1] : null
  const value = latest ? (latest[field.key as keyof ServerMetricPoint] as number) : null
  const sev = value != null && field.severity ? field.severity(value) : null

  const series: { label: string; values: (number | null)[]; color: { light: string; dark: string }; dashed?: boolean }[] = [
    { label: field.label, values: windowed.map((p) => p[field.key as keyof ServerMetricPoint] as number), color: SERIES_BLUE },
  ]
  const paired = field.paired
  if (paired) {
    series.push({
      label: paired.label,
      values: windowed.map((p) => p[paired.key as keyof ServerMetricPoint] as number),
      color: SERIES_AQUA,
      dashed: true,
    })
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={fieldKey}
          onChange={(e) => setFieldKey(e.target.value)}
          className="h-8 rounded-md border border-border bg-transparent px-2 text-xs"
          aria-label="Metric to watch live"
        >
          {FIELDS.map((f) => (
            <option key={f.key} value={f.key}>
              {f.label}
            </option>
          ))}
        </select>
        <span className="flex items-baseline gap-1.5">
          {value != null ? (
            <>
              <span className={cn('text-xl font-semibold tabular-nums', sev && SEVERITY_TEXT[sev])}>{field.format(value)}</span>
              <span className="text-xs text-muted-foreground">{field.label} — live, 10s refresh</span>
            </>
          ) : (
            <span className="text-xs text-muted-foreground">waiting for the next sample…</span>
          )}
        </span>
      </div>
      <MetricChart
        times={windowed.map((p) => p.ts)}
        series={series}
        format={field.format}
        yMax={field.max}
        height={160}
      />
      <p className="text-[11px] text-muted-foreground">
        Last 30 minutes · every agent sample (1/min). {liveQuery.isFetching && 'Refreshing…'}
      </p>
    </div>
  )
}

/** Per-target ping panel — each target gets its own latency + loss readout
 * and trend, so "which path degraded" is answered at a glance. */
export function PingPanel({ points }: { points: ServerMetricPoint[] }) {
  const targets = React.useMemo(() => {
    const names = new Set<string>()
    for (const p of points) {
      for (const t of Object.keys(p.extra?.ping ?? {})) names.add(t)
    }
    return [...names]
  }, [points])

  if (targets.length === 0) {
    return <p className="text-xs text-muted-foreground">No ping targets configured on this server's agent.</p>
  }

  return (
    <div className="flex flex-col gap-3">
      {targets.map((t) => {
        const vals = points.map((p) => (p.extra?.ping?.[t] ?? null))
        const latest = [...vals].reverse().find((r) => r != null) ?? null
        return (
          <div key={t} className="flex flex-col gap-1">
            <div className="flex items-center justify-between gap-2 text-xs">
              <span className="font-mono">{t}</span>
              {latest && (
                <span className="flex items-baseline gap-2">
                  <span className="font-semibold tabular-nums">{latest.avg_ms != null ? `${Math.round(latest.avg_ms)} ms` : '—'}</span>
                  <span className={cn('tabular-nums', latest.loss > 0 ? 'text-destructive' : 'text-success')}>
                    {latest.loss.toFixed(0)}% loss
                  </span>
                </span>
              )}
            </div>
            <MetricChart
              times={points.map((p) => p.ts)}
              format={(v) => `${v.toFixed(0)} ms`}
              series={[{ label: 'avg ms', values: vals.map((r) => (r?.avg_ms != null ? r.avg_ms : null)), color: SERIES_BLUE }]}
              height={90}
            />
          </div>
        )
      })}
    </div>
  )
}
