import * as React from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { monitorApi } from '@/lib/api'
import type { MonitorSeverity } from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { MetricChart, SERIES_AQUA, SERIES_BLUE } from '@/components/monitor/MetricChart'
import { cn, formatAgo } from '@/lib/utils'

const SEVERITY_DOT: Record<MonitorSeverity, string> = {
  critical: 'bg-destructive',
  warn: 'bg-warning',
  info: 'bg-muted-foreground/50',
}

function formatMbps(v: number): string {
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}G`
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  return `${(v / 1e3).toFixed(0)}K`
}

const HOUR_CHOICES = [6, 24, 168] as const

export function ServerDetailPage() {
  const { serverId = '' } = useParams()
  const [hours, setHours] = React.useState<(typeof HOUR_CHOICES)[number]>(24)

  React.useEffect(() => {
    document.title = `Reseller Console | ${serverId}`
  }, [serverId])

  const historyQuery = useQuery({
    queryKey: ['monitor', 'history', serverId, hours],
    queryFn: () => monitorApi.history(serverId, hours),
    refetchInterval: 60_000,
  })
  const eventsQuery = useQuery({
    queryKey: ['monitor', 'events', 'server', serverId],
    queryFn: () => monitorApi.events({ server_id: serverId, limit: 200 }),
    refetchInterval: 30_000,
  })

  const points = historyQuery.data ?? []
  const events = eventsQuery.data ?? []
  const times = points.map((p) => p.ts)
  const latest = points.length > 0 ? points[points.length - 1] : null
  const pingEntries = Object.entries(latest?.extra?.ping ?? {})

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <Button size="icon-sm" variant="ghost" asChild>
            <Link to="/servers" aria-label="Back to servers">
              <ArrowLeft className="h-4 w-4" />
            </Link>
          </Button>
          <div>
            <h1 className="font-mono text-lg font-semibold tracking-tight">{serverId}</h1>
            <p className="text-xs text-muted-foreground">
              {points.length > 0 ? `${points.length} samples · newest ${formatAgo(latest!.ts)}` : 'no samples yet'}
              {latest && ` · load ${latest.load1.toFixed(2)} on ${latest.cpu_cores} cores`}
            </p>
          </div>
        </div>
        <Tabs value={String(hours)} onValueChange={(v) => setHours(Number(v) as (typeof HOUR_CHOICES)[number])}>
          <TabsList>
            {HOUR_CHOICES.map((h) => (
              <TabsTrigger key={h} value={String(h)}>
                {h === 168 ? '7d' : `${h}h`}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>

      {historyQuery.isLoading ? (
        <div className="grid gap-4 lg:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[240px]" />
          ))}
        </div>
      ) : points.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          No samples from this server in the window. Either it is a brand-new agent or the ingest token changed.
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader className="pb-1">
              <CardTitle>
                CPU {latest!.cpu_pct.toFixed(0)}% · steal {latest!.steal_pct.toFixed(0)}%
              </CardTitle>
            </CardHeader>
            <CardContent>
              <MetricChart
                times={times}
                yMax={100}
                format={(v) => `${v.toFixed(0)}%`}
                series={[
                  { label: 'cpu %', values: points.map((p) => p.cpu_pct), color: SERIES_BLUE },
                  { label: 'steal %', values: points.map((p) => p.steal_pct), color: SERIES_AQUA, dashed: true },
                ]}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-1">
              <CardTitle>
                Memory {(latest!.mem_used_mb / 1024).toFixed(1)}/{(latest!.mem_total_mb / 1024).toFixed(0)}G
                {latest!.swap_used_mb > 0 && <span className="ml-2 font-normal text-warning">swap {(latest!.swap_used_mb / 1024).toFixed(1)}G</span>}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <MetricChart
                times={times}
                format={(v) => `${(v / 1024).toFixed(1)}G`}
                series={[
                  { label: 'used GB', values: points.map((p) => p.mem_used_mb), color: SERIES_BLUE },
                  { label: 'available GB', values: points.map((p) => p.mem_avail_mb), color: SERIES_AQUA, dashed: true },
                ]}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-1">
              <CardTitle>
                Network ↓{formatMbps(latest!.net_rx_bps)}ps · ↑{formatMbps(latest!.net_tx_bps)}ps
              </CardTitle>
            </CardHeader>
            <CardContent>
              <MetricChart
                times={times}
                format={formatMbps}
                series={[
                  { label: 'rx', values: points.map((p) => p.net_rx_bps), color: SERIES_BLUE },
                  { label: 'tx', values: points.map((p) => p.net_tx_bps), color: SERIES_AQUA },
                ]}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-1">
              <CardTitle>
                TCP retransmit {latest!.tcp_retrans_pct.toFixed(1)}%
                {pingEntries.length > 0 && (
                  <span className="ml-2 font-normal text-muted-foreground">
                    {pingEntries
                      .map(([t, r]) => `${t.split('.')[0]}: ${r?.avg_ms != null ? `${Math.round(r.avg_ms)}ms` : '—'}${r && r.loss > 0 ? ` (${r.loss.toFixed(0)}% loss)` : ''}`)
                      .join(' · ')}
                  </span>
                )}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <MetricChart
                times={times}
                format={(v) => `${v.toFixed(1)}%`}
                series={[{ label: 'retransmit %', values: points.map((p) => p.tcp_retrans_pct), color: SERIES_BLUE }]}
              />
            </CardContent>
          </Card>
        </div>
      )}

      <Card>
        <CardHeader className="pb-1">
          <CardTitle>Events — {serverId}</CardTitle>
        </CardHeader>
        <CardContent className="pb-2">
          {events.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">Nothing anomalous recorded for this server.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[90px]">when</TableHead>
                  <TableHead className="w-[16px]" />
                  <TableHead className="w-[220px]">type</TableHead>
                  <TableHead>detail</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.map((e, i) => (
                  <TableRow key={e.ts + e.type + i}>
                    <TableCell className="text-xs text-muted-foreground" title={e.ts}>
                      {formatAgo(e.ts)}
                    </TableCell>
                    <TableCell>
                      <span aria-hidden className={cn('block h-1.5 w-1.5 rounded-full', SEVERITY_DOT[e.severity])} title={e.severity} />
                    </TableCell>
                    <TableCell className="font-mono text-xs">{e.type}</TableCell>
                    <TableCell className="text-xs">{e.detail}</TableCell>
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
