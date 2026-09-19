import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueries } from '@tanstack/react-query'
import { monitorApi } from '@/lib/api'
import type { MonitorSeverity, ServerSummary } from '@/lib/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { UsageBar } from '@/components/UsageBar'
import { Sparkline } from '@/components/monitor/MetricChart'
import { cn, formatAgo } from '@/lib/utils'

/** "8w 1d" style uptime from seconds — coarser than formatAgo on purpose:
 * weeks are the unit you think about a VPN box's uptime in. */
function formatUptime(s: number): string {
  if (!s) return '—'
  const d = Math.floor(s / 86400)
  const w = Math.floor(d / 7)
  if (w > 0) return `${w}w ${d % 7}d`
  if (d > 0) return `${d}d ${Math.floor((s % 86400) / 3600)}h`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

function formatMbps(bps: number): string {
  if (bps >= 1e9) return `${(bps / 1e9).toFixed(1)} Gbps`
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mbps`
  return `${(bps / 1e3).toFixed(0)} Kbps`
}

const STATUS_DOT: Record<string, string> = {
  online: 'bg-success',
  stale: 'bg-warning',
  unknown: 'bg-muted-foreground/40',
}

const SEVERITY_DOT: Record<MonitorSeverity, string> = {
  critical: 'bg-destructive',
  warn: 'bg-warning',
  info: 'bg-muted-foreground/50',
}

function ServerCard({ server, cpuHistory }: { server: ServerSummary; cpuHistory: (number | null)[] }) {
  const navigate = useNavigate()
  const memPct = server.mem_total_mb > 0 ? (server.mem_used_mb / server.mem_total_mb) * 100 : null
  const pingEntries = Object.entries(server.extra?.ping ?? {})
  const ping =
    pingEntries.length > 0
      ? pingEntries
          .map(([t, r]) => (r?.avg_ms != null ? `${t.split('.')[0]} ${Math.round(r.avg_ms)}ms` : `${t.split('.')[0]} —`))
          .join(' · ')
      : null
  const conntrackPct = server.conntrack_max > 0 ? Math.round((server.conntrack_count / server.conntrack_max) * 100) : null

  return (
    <Card
      className="cursor-pointer transition-colors hover:border-primary/50"
      onClick={() => navigate(`/servers/${encodeURIComponent(server.server_id)}`)}
    >
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="font-mono">{server.server_id}</CardTitle>
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground" title={`last sample ${formatAgo(server.ts)}`}>
            <span aria-hidden className={cn('h-1.5 w-1.5 rounded-full', STATUS_DOT[server.status])} />
            {server.status}
          </span>
        </div>
        <p className="text-[11px] text-muted-foreground">
          up {formatUptime(server.uptime_s)} · sample {formatAgo(server.ts)}
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-2.5 pb-3">
        <div className="flex items-end justify-between gap-3">
          <div>
            <p className="text-xs text-muted-foreground">CPU</p>
            <p className="text-lg font-semibold leading-tight tabular-nums">
              {server.cpu_pct.toFixed(0)}%
              {server.steal_pct >= 5 && (
                <span className="ml-1.5 text-xs font-medium text-warning" title="steal — host oversold or noisy neighbor">
                  steal {server.steal_pct.toFixed(0)}%
                </span>
              )}
            </p>
          </div>
          <Sparkline values={cpuHistory} />
        </div>

        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          <div className="flex justify-between">
            <span className="text-muted-foreground">mem</span>
            <span className="tabular-nums">{server.mem_total_mb > 0 ? `${(server.mem_used_mb / 1024).toFixed(1)}/${(server.mem_total_mb / 1024).toFixed(0)}G` : '—'}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted-foreground">disk</span>
            <span className="tabular-nums">{server.disk_used_pct.toFixed(0)}%</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted-foreground">↓ {formatMbps(server.net_rx_bps)}</span>
            <span className="tabular-foreground tabular-nums">↑ {formatMbps(server.net_tx_bps)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted-foreground">load</span>
            <span className="tabular-nums">{server.load1.toFixed(2)}</span>
          </div>
        </div>

        {memPct != null && <UsageBar used={server.mem_used_mb * 1e6} limit={server.mem_total_mb * 1e6} />}
        {server.disk_used_pct >= 80 && <UsageBar used={server.disk_used_pct} limit={100} />}

        <div className="flex flex-wrap items-center justify-between gap-1 text-[11px] text-muted-foreground">
          <span title="warn+critical events in the last 24h">
            {server.warn_events_24h > 0 ? (
              <span className={server.warn_events_24h > 5 ? 'font-medium text-destructive' : 'font-medium text-warning'}>
                {server.warn_events_24h} alerts/24h
              </span>
            ) : (
              <span className="text-success">no alerts/24h</span>
            )}
          </span>
          {conntrackPct != null && <span className="tabular-nums" title="conntrack table usage">ct {conntrackPct}%</span>}
          {ping && <span className="truncate tabular-nums" title="latency toward ping targets">{ping}</span>}
        </div>

        {server.last_event && (
          <p className="truncate text-[11px] text-muted-foreground">
            <span aria-hidden className={cn('mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle', SEVERITY_DOT[server.last_event.severity])} />
            {formatAgo(server.last_event.ts)} · {server.last_event.type}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

export function ServersPage() {
  React.useEffect(() => {
    document.title = 'Reseller Console | Servers'
  }, [])

  const serversQuery = useQuery({ queryKey: ['monitor', 'servers'], queryFn: monitorApi.servers, refetchInterval: 30_000 })
  const [severityFilter, setSeverityFilter] = React.useState<'all' | 'alerts'>('all')

  const servers = serversQuery.data ?? []

  // One 1h CPU history per server for the card sparklines — 5 small parallel
  // queries, refreshed with the cards. Parallel by react-query's useQueries.
  const histories = useQueries({
    queries: servers.map((s) => ({
      queryKey: ['monitor', 'history', s.server_id, 1],
      queryFn: () => monitorApi.history(s.server_id, 1),
      refetchInterval: 60_000,
    })),
  })

  const eventsQuery = useQuery({
    queryKey: ['monitor', 'events', severityFilter],
    queryFn: () => monitorApi.events({ limit: 100 }),
    refetchInterval: 30_000,
  })
  const events = (eventsQuery.data ?? []).filter((e) =>
    severityFilter === 'all' ? true : e.severity !== 'info',
  )

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">Servers</h1>
        <p className="text-xs text-muted-foreground">
          Live health of every box in the fleet — one-minute samples pushed by each server's agent. Click a card for history and its event log.
        </p>
      </div>

      {serversQuery.isLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-[220px]" />
          ))}
        </div>
      ) : servers.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          No server has reported yet. Agents push to <span className="font-mono">/api/monitor/ingest</span> once a
          minute — see <span className="font-mono">scripts/monitor/README.md</span> if this stays empty.
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {servers.map((s, i) => (
            <ServerCard key={s.server_id} server={s} cpuHistory={(histories[i]?.data ?? []).map((p) => p.cpu_pct)} />
          ))}
        </div>
      )}

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle>Events — the outage timeline</CardTitle>
          <Tabs value={severityFilter} onValueChange={(v) => setSeverityFilter(v as 'all' | 'alerts')}>
            <TabsList>
              <TabsTrigger value="all">All</TabsTrigger>
              <TabsTrigger value="alerts">Alerts only</TabsTrigger>
            </TabsList>
          </Tabs>
        </CardHeader>
        <CardContent className="pb-2">
          {events.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">
              {severityFilter === 'alerts' ? 'No alerts — nothing anomalous recorded.' : 'Nothing recorded yet.'}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[70px]">when</TableHead>
                  <TableHead className="w-[16px]" />
                  <TableHead className="w-[130px]">server</TableHead>
                  <TableHead className="w-[210px]">type</TableHead>
                  <TableHead>detail</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.slice(0, 30).map((e, i) => (
                  <TableRow key={e.ts + e.type + i}>
                    <TableCell className="text-xs text-muted-foreground">{formatAgo(e.ts)}</TableCell>
                    <TableCell>
                      <span aria-hidden className={cn('block h-1.5 w-1.5 rounded-full', SEVERITY_DOT[e.severity])} title={e.severity} />
                    </TableCell>
                    <TableCell className="font-mono text-xs">{e.server_id}</TableCell>
                    <TableCell className="font-mono text-xs">{e.type}</TableCell>
                    <TableCell className="max-w-[420px] truncate text-xs" title={e.detail}>{e.detail}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
