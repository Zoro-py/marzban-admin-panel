import * as React from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { CalendarCheck, CheckCircle2, PlayCircle } from 'lucide-react'
import { paygMonthlyApi, apiErrorMessage } from '@/lib/api'
import { StatCard } from '@/components/StatCard'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Money } from '@/components/Money'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { formatDateTime } from '@/lib/utils'

export function MonthlySettlementsPage() {
  React.useEffect(() => {
    document.title = 'Shiraze | Monthly Settlements'
  }, [])

  const queryClient = useQueryClient()
  const openAccount = useOpenAccountInspector()
  const [period, setPeriod] = React.useState<string | undefined>(undefined)

  const periodsQuery = useQuery({ queryKey: ['payg-monthly', 'periods'], queryFn: paygMonthlyApi.periods })
  const batchesQuery = useQuery({
    queryKey: ['payg-monthly', 'batches', period ?? periodsQuery.data?.[0] ?? null],
    queryFn: () => paygMonthlyApi.batches(period),
    enabled: periodsQuery.isSuccess,
  })

  const runMutation = useMutation({
    mutationFn: paygMonthlyApi.run,
    onSuccess: (result) => {
      if (!result.ran) {
        toast.info('Nothing unsettled yet — already up to date for this period.')
        return
      }
      toast.success(`Settled ${result.settled ?? 0} of ${(result.groups ?? 0) + (result.accounts ?? 0)} for ${result.period} — ${result.total?.toLocaleString('en-US')} T total`)
      queryClient.invalidateQueries({ queryKey: ['payg-monthly'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const markPaidMutation = useMutation({
    mutationFn: paygMonthlyApi.markPaid,
    onSuccess: () => {
      toast.success('Marked as paid')
      queryClient.invalidateQueries({ queryKey: ['payg-monthly'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const rows = batchesQuery.data?.rows ?? []
  const activePeriod = batchesQuery.data?.period ?? null
  const totalAmount = rows.reduce((sum, r) => sum + r.amount, 0)
  const paidCount = rows.filter((r) => r.marked_paid_at !== null).length
  const unpaidAmount = rows.filter((r) => r.marked_paid_at === null).reduce((sum, r) => sum + r.amount, 0)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Monthly Settlements</h1>
          <p className="text-xs text-muted-foreground">
            Pay-as-you-go groups and standalone accounts, settled automatically on the last night of each Jalali
            month. Mark a period paid here once the money's actually in — separately from when it was charged.
          </p>
        </div>
        <Button
          size="sm"
          variant="outline"
          className="gap-1.5"
          onClick={() => runMutation.mutate()}
          disabled={runMutation.isPending}
        >
          <PlayCircle className="h-4 w-4" />
          {runMutation.isPending ? 'Checking…' : 'Run check now'}
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <StatCard label="This period, billed" value={`${totalAmount.toLocaleString('en-US')} T`} />
        <StatCard
          label="Still unpaid"
          value={`${unpaidAmount.toLocaleString('en-US')} T`}
          tone={unpaidAmount > 0 ? 'destructive' : 'success'}
        />
        <StatCard label="Settlements this period" value={rows.length} />
        <StatCard label="Marked paid" value={`${paidCount} / ${rows.length}`} tone={paidCount === rows.length && rows.length > 0 ? 'success' : undefined} />
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
          <h2 className="flex items-center gap-1.5 text-[13px] font-semibold">
            <CalendarCheck className="h-3.5 w-3.5 text-muted-foreground" />
            Settlements
          </h2>
          {periodsQuery.data && periodsQuery.data.length > 0 && (
            <Select value={activePeriod ?? undefined} onValueChange={setPeriod}>
              <SelectTrigger className="w-[140px]">
                <SelectValue placeholder="Period" />
              </SelectTrigger>
              <SelectContent>
                {periodsQuery.data.map((p) => (
                  <SelectItem key={p} value={p}>
                    {p}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Who</TableHead>
              <TableHead className="hidden sm:table-cell">Type</TableHead>
              <TableHead className="text-right">Usage</TableHead>
              <TableHead className="text-right">Amount</TableHead>
              <TableHead className="hidden md:table-cell">Settled</TableHead>
              <TableHead className="text-right">Paid</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {periodsQuery.isSuccess && periodsQuery.data.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="py-6 text-center text-muted-foreground">
                  No monthly settlements yet — they'll appear here after the first Jalali month-end run.
                </TableCell>
              </TableRow>
            )}
            {rows.length === 0 && periodsQuery.data && periodsQuery.data.length > 0 && (
              <TableRow>
                <TableCell colSpan={6} className="py-6 text-center text-muted-foreground">
                  No settlements for this period.
                </TableCell>
              </TableRow>
            )}
            {rows.map((r) => (
              <TableRow
                key={r.id}
                className="cursor-pointer"
                onClick={() => (r.account_id !== null ? openAccount(r.account_id) : undefined)}
              >
                <TableCell className="max-w-[160px] truncate font-medium">
                  {r.group_id !== null ? (
                    <Link
                      to={`/groups/${r.group_id}`}
                      className="hover:underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {r.display_name}
                    </Link>
                  ) : (
                    r.display_name
                  )}
                </TableCell>
                <TableCell className="hidden sm:table-cell">
                  <Badge variant="secondary">{r.group_id !== null ? 'group' : 'account'}</Badge>
                </TableCell>
                <TableCell className="text-right text-xs tabular-nums text-muted-foreground">{r.billable_gb.toFixed(2)} GB</TableCell>
                <TableCell className="text-right">
                  <Money amount={r.amount} kind="plain" />
                </TableCell>
                <TableCell className="hidden text-xs text-muted-foreground md:table-cell">{formatDateTime(r.settled_at)}</TableCell>
                <TableCell className="text-right" onClick={(e) => e.stopPropagation()}>
                  {r.marked_paid_at !== null ? (
                    <Badge variant="success" className="gap-1">
                      <CheckCircle2 className="h-3 w-3" /> paid
                    </Badge>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={markPaidMutation.isPending}
                      onClick={() => markPaidMutation.mutate(r.id)}
                    >
                      Mark as paid
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
