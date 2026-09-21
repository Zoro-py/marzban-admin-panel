import * as React from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { apiErrorMessage, historyApi } from '@/lib/api'
import { EmptyState } from '@/components/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { AccountPicker, MAX_ACCOUNTS } from '@/components/history/AccountPicker'
import { ChargeTable } from '@/components/history/ChargeTable'
import { ChargeTimeline } from '@/components/history/ChargeTimeline'
import { CumulativeChart } from '@/components/history/CumulativeChart'
import { SummaryTiles } from '@/components/history/SummaryTiles'
import { DEFAULT_RANGE, RangePicker, isRangeValid, resolveRange } from '@/components/history/RangePicker'
import type { RangeCode, RangeValue } from '@/components/history/RangePicker'

const LS_KEY = 'vpn_history_last'
const RANGE_CODES: RangeCode[] = ['30d', '90d', '6m', 'jm', 'all', 'custom']
const YMD = /^\d{4}-\d{2}-\d{2}$/

interface HistoryState {
  ids: number[]
  range: RangeValue
  credits: boolean
}

/** The URL is the state — anything malformed silently degrades to defaults
 * rather than throwing (a shared/garbled link must never blank the page). */
function parseState(params: URLSearchParams): HistoryState {
  const ids = [...new Set((params.get('a') ?? '').split(',').filter((s) => /^\d+$/.test(s)).map(Number))].slice(
    0,
    MAX_ACCOUNTS,
  )
  const code = params.get('r')
  const range: RangeValue =
    code && RANGE_CODES.includes(code as RangeCode)
      ? {
          code: code as RangeCode,
          customSince: code === 'custom' && YMD.test(params.get('cs') ?? '') ? params.get('cs')! : '',
          customUntil: code === 'custom' && YMD.test(params.get('cu') ?? '') ? params.get('cu')! : '',
        }
      : DEFAULT_RANGE
  return { ids, range, credits: params.get('credits') === '1' }
}

/** Only meaningful params are written: no `a` when nothing is selected, cs/cu
 * only for custom ranges, and the default range (6m) stays implicit. */
function buildParams(state: HistoryState): URLSearchParams {
  const params = new URLSearchParams()
  if (state.ids.length > 0) params.set('a', state.ids.join(','))
  if (state.range.code !== DEFAULT_RANGE.code || state.range.customSince || state.range.customUntil)
    params.set('r', state.range.code)
  if (state.range.code === 'custom') {
    if (state.range.customSince) params.set('cs', state.range.customSince)
    if (state.range.customUntil) params.set('cu', state.range.customUntil)
  }
  if (state.credits) params.set('credits', '1')
  return params
}

function loadLast(): URLSearchParams | null {
  try {
    const raw = window.localStorage.getItem(LS_KEY)
    return raw ? new URLSearchParams(raw) : null
  } catch {
    return null
  }
}

function saveLast(params: URLSearchParams) {
  try {
    window.localStorage.setItem(LS_KEY, params.toString())
  } catch {
    // Private mode / quota — the URL still carries the full state.
  }
}

export function ChargeHistoryPage() {
  React.useEffect(() => {
    document.title = 'Shiraze | History'
  }, [])

  const [searchParams, setSearchParams] = useSearchParams()
  const state = React.useMemo(() => parseState(searchParams), [searchParams])

  // First visit with no `a` in the URL: bring back the last session's picks.
  // One-shot — later losing the `a` param (clearing the selection) must not
  // "restore" it right back.
  const restoredOnce = React.useRef(false)
  React.useEffect(() => {
    if (restoredOnce.current) return
    restoredOnce.current = true
    if (searchParams.has('a')) return
    const saved = loadLast()
    if (saved) setSearchParams(saved, { replace: true })
  }, [searchParams, setSearchParams])

  function apply(next: HistoryState) {
    const params = buildParams(next)
    setSearchParams(params, { replace: true })
    saveLast(params)
  }
  const setIds = (ids: number[]) => apply({ ...state, ids })
  const patchRange = (patch: Partial<RangeValue>) => apply({ ...state, range: { ...state.range, ...patch } })
  const setCredits = (credits: boolean) => apply({ ...state, credits })

  const accountsQuery = useQuery({ queryKey: ['history', 'accounts'], queryFn: historyApi.accounts })

  const rangeUsable = isRangeValid(state.range)
  const chargesQuery = useQuery({
    queryKey: ['history', 'charges', state.ids.join(','), state.range.code, state.range.customSince, state.range.customUntil, state.credits],
    enabled: state.ids.length > 0 && rangeUsable,
    queryFn: () => {
      const { since, until } = resolveRange(state.range)
      return historyApi.charges({ account_ids: state.ids.join(','), since, until, include_credits: state.credits })
    },
  })

  // resolveRange yields date-only LOCAL strings; the backend reads `until` as
  // through the END of that day, so the chart axis closes at local 23:59:59.999.
  const resolvedWindow = resolveRange(state.range)
  const sinceMs = Date.parse(`${resolvedWindow.since}T00:00:00`)
  const untilMs = Date.parse(`${resolvedWindow.until}T23:59:59.999`)

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">History</h1>
        <p className="text-xs text-muted-foreground">
          Read-only charge history per account — ledger entries, packages and payments over any window.
        </p>
      </div>

      <AccountPicker options={accountsQuery.data ?? []} selected={state.ids} onChange={setIds} />

      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
        <div className="min-w-0 flex-1">
          <RangePicker value={state.range} onChange={patchRange} />
        </div>
        <label className="flex cursor-pointer select-none items-center gap-2 pt-0.5 text-xs">
          <Checkbox checked={state.credits} onCheckedChange={(v) => setCredits(v === true)} aria-label="Show payments" />
          Show payments
        </label>
      </div>

      {accountsQuery.isError && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          Couldn't load accounts — {apiErrorMessage(accountsQuery.error)}
        </p>
      )}

      {chargesQuery.isError && (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          Couldn't load charges — {apiErrorMessage(chargesQuery.error)}
        </p>
      )}

      {state.ids.length === 0 ? (
        <EmptyState
          title="No accounts selected."
          description="Pick one or more accounts above — deleted accounts keep their history here."
        />
      ) : !rangeUsable ? (
        <EmptyState title="Waiting on a custom range." description="Set both From and To dates above to load this window." />
      ) : chargesQuery.isLoading ? (
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {Array.from({ length: 4 }).map((_, i) => (
              <Skeleton key={i} className="h-[92px] rounded-lg" />
            ))}
          </div>
          <Card>
            <CardContent className="pt-3">
              <Skeleton className="h-64 w-full" />
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-3">
              <Skeleton className="h-64 w-full" />
            </CardContent>
          </Card>
        </div>
      ) : chargesQuery.data ? (
        <>
          <SummaryTiles data={chargesQuery.data} />
          <Card>
            <CardHeader className="pb-1">
              <CardTitle>Charge timeline</CardTitle>
            </CardHeader>
            <CardContent>
              <ChargeTimeline data={chargesQuery.data} sinceMs={sinceMs} untilMs={untilMs} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-1">
              <CardTitle>Cumulative charges</CardTitle>
            </CardHeader>
            <CardContent>
              <CumulativeChart data={chargesQuery.data} sinceMs={sinceMs} untilMs={untilMs} />
            </CardContent>
          </Card>
          <ChargeTable data={chargesQuery.data} />
        </>
      ) : null}
    </div>
  )
}
