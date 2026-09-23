import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { apiErrorMessage, historyApi } from '@/lib/api'
import { Skeleton } from '@/components/ui/skeleton'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { CumulativeChart } from './CumulativeChart'
import { DEFAULT_RANGE, resolveRange } from './RangePicker'

/** Compact cumulative-charge chart for a fixed 6-month window — the glance
 * version of /history for the detail pages (single account, a customer's or a
 * group's members). Same fetch and window derivation as ChargeHistoryPage,
 * minus every control: no pickers, no table, just the chart and a deep link
 * that lands the full page pre-filtered to the same scope. */
export function ChargeHistoryPreview({ accountIds, title = 'Charge history' }: { accountIds: number[]; title?: string }) {
  const ids = accountIds.join(',')
  // Identical key shape to ChargeHistoryPage's charges query at its defaults
  // (6m, no credits) — the preview and the full page share one cache entry
  // when their scopes match. The joined ids are IN the key, so different
  // scopes never see each other's data.
  const chargesQuery = useQuery({
    queryKey: ['history', 'charges', ids, DEFAULT_RANGE.code, DEFAULT_RANGE.customSince, DEFAULT_RANGE.customUntil, false],
    enabled: accountIds.length > 0,
    queryFn: () => {
      const { since, until } = resolveRange(DEFAULT_RANGE)
      return historyApi.charges({ account_ids: ids, since, until, include_credits: false })
    },
  })

  // resolveRange yields date-only LOCAL strings; the backend reads `until` as
  // through the END of that day, so the chart axis closes at local 23:59:59.999.
  // (Same computation as ChargeHistoryPage — the two windows must agree.)
  const resolvedWindow = resolveRange(DEFAULT_RANGE)
  const sinceMs = Date.parse(`${resolvedWindow.since}T00:00:00`)
  const untilMs = Date.parse(`${resolvedWindow.until}T23:59:59.999`)

  if (accountIds.length === 0) return null

  // include_credits is false, so any entry the API returns is a charge inside
  // the requested window — mirror the chart's own "has anything to plot" test.
  const hasCharges = (chargesQuery.data?.entries ?? []).some((e) => e.type === 'charge')

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 pb-2">
        <CardTitle>
          {title} <span className="font-normal text-muted-foreground">· last 6 months</span>
        </CardTitle>
        {/* `a` and `r` are the same params ChargeHistoryPage's parseState reads
            (and buildParams writes) — any other names would silently reset the
            page to its defaults instead of landing pre-filtered. */}
        <Link to={`/history?a=${ids}&r=6m`} className="shrink-0 text-xs text-muted-foreground hover:text-foreground hover:underline">
          Open full history →
        </Link>
      </CardHeader>
      <CardContent>
        {chargesQuery.isError ? (
          <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            Couldn't load charges — {apiErrorMessage(chargesQuery.error)}
          </p>
        ) : chargesQuery.isLoading ? (
          <Skeleton className="h-64 w-full" />
        ) : chargesQuery.data && hasCharges ? (
          <CumulativeChart data={chargesQuery.data} sinceMs={sinceMs} untilMs={untilMs} />
        ) : (
          <p className="py-6 text-center text-xs text-muted-foreground">No charges in the last 6 months.</p>
        )}
      </CardContent>
    </Card>
  )
}
