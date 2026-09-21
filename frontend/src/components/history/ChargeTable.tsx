import * as React from 'react'
import { Download } from 'lucide-react'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { SortableHeader, nextSort, type SortState } from '@/components/ui/sortable-header'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { EmptyState } from '@/components/EmptyState'
import type { ChargeHistory } from '@/lib/types'
import { cn, formatToman, parseDate } from '@/lib/utils'
import { formatJalali } from '@/lib/jalali'

/** "18.00 GB" → "18 GB"; null stays null so the caller renders "—". */
function fmtGb(gb: number | null): string | null {
  return gb == null ? null : String(Number(gb.toFixed(2)))
}

interface ChargeTableProps {
  data: ChargeHistory
}

/** Every ledger row in the window, sortable, with a CSV export (UTF-8 + BOM so
 * Persian notes survive Excel; both calendars in the date columns). The table
 * is the ground truth the charts summarize — same rows, tabular form. */
export function ChargeTable({ data }: ChargeTableProps) {
  const [sort, setSort] = React.useState<SortState | null>({ key: 'date', dir: 'desc' })

  const names = React.useMemo(() => new Map(data.accounts.map((a) => [a.id, a.username])), [data.accounts])

  const rows = React.useMemo(() => {
    const copy = [...data.entries]
    const key = sort?.key
    const dir = sort?.dir === 'asc' ? 1 : -1
    if (!key) return copy
    copy.sort((a, b) => {
      let cmp = 0
      if (key === 'date') cmp = a.date.localeCompare(b.date)
      else if (key === 'account') cmp = (names.get(a.account_id) ?? '').localeCompare(names.get(b.account_id) ?? '')
      else if (key === 'type') cmp = a.type.localeCompare(b.type)
      else if (key === 'amount') cmp = a.amount - b.amount
      else if (key === 'gb') cmp = (a.gb_amount ?? -1) - (b.gb_amount ?? -1)
      else if (key === 'source') cmp = a.source.localeCompare(b.source)
      return cmp * dir
    })
    return copy
  }, [data.entries, sort, names])

  function onSort(key: string) {
    setSort((cur) => nextSort(cur, key))
  }

  function exportCsv() {
    const esc = (v: string | number | null | undefined) => `"${String(v ?? '').replaceAll('"', '""')}"`
    const lines = [['Date (UTC)', 'Date (Jalali)', 'Date (Gregorian)', 'Account', 'Type', 'Amount (Toman)', 'GB', 'Source', 'Operator', 'Note']
      .join(',')]
    for (const r of rows) {
      const d = parseDate(r.date)
      lines.push(
        [
          r.date,
          formatJalali(d),
          d.toISOString().slice(0, 10),
          names.get(r.account_id) ?? String(r.account_id),
          r.type,
          r.amount,
          r.gb_amount ?? '',
          r.source,
          r.created_by ?? '',
          r.note ?? '',
        ]
          .map(esc)
          .join(','),
      )
    }
    // \ufeff = UTF-8 BOM: without it Excel opens the file as ANSI and Persian
    // note text turns to mojibake.
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'charge-history.csv'
    a.click()
    URL.revokeObjectURL(url)
  }

  if (data.entries.length === 0) {
    return <EmptyState title="No charges in this period" description="Widen the range or turn on payments to see credit rows." />
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-end">
        <Button size="sm" variant="outline" onClick={exportCsv} className="h-7 gap-1.5 text-xs">
          <Download className="h-3.5 w-3.5" />
          CSV ({rows.length})
        </Button>
      </div>
      <div className="overflow-x-auto">
        <Table>
          <TableHeader>
            <TableRow>
              <SortableHeader label="Date" sortKey="date" sort={sort} onSort={onSort} />
              <SortableHeader label="Account" sortKey="account" sort={sort} onSort={onSort} />
              <SortableHeader label="Type" sortKey="type" sort={sort} onSort={onSort} />
              <SortableHeader label="Toman" sortKey="amount" sort={sort} onSort={onSort} align="right" />
              <SortableHeader label="GB" sortKey="gb" sort={sort} onSort={onSort} align="right" />
              <SortableHeader label="Source" sortKey="source" sort={sort} onSort={onSort} />
              <TableHead>Operator</TableHead>
              <TableHead>Note</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => {
              const d = parseDate(r.date)
              const gb = fmtGb(r.gb_amount)
              return (
                <TableRow key={r.id}>
                  <TableCell className="whitespace-nowrap">
                    <span className="tabular-nums">{d.toISOString().slice(0, 10)} {d.toISOString().slice(11, 16)}</span>
                    <span className="ml-1.5 text-[11px] text-muted-foreground">{formatJalali(d)}</span>
                  </TableCell>
                  <TableCell className="max-w-[160px] truncate font-mono text-[12px]">{names.get(r.account_id) ?? r.account_id}</TableCell>
                  <TableCell>
                    <Badge variant={r.type === 'charge' ? 'destructive' : 'success'}>{r.type}</Badge>
                  </TableCell>
                  <TableCell
                    className={cn('text-right tabular-nums', r.type === 'charge' ? 'text-destructive' : 'text-success')}
                  >
                    {r.type === 'charge' ? '+' : '−'}
                    {formatToman(r.amount)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{gb ?? <span className="text-muted-foreground">—</span>}</TableCell>
                  <TableCell className="text-muted-foreground">{r.source}</TableCell>
                  <TableCell className="text-muted-foreground">{r.created_by ?? '—'}</TableCell>
                  <TableCell className="max-w-[220px] truncate text-muted-foreground" title={r.note ?? undefined}>
                    {r.note ?? '—'}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
