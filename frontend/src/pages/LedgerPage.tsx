import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Ban, CircleSlash2, Download, RotateCcw, Search } from 'lucide-react'
import { apiErrorMessage, customersApi, groupsApi, historyApi, ledgerApi } from '@/lib/api'
import type { LedgerEntry } from '@/lib/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { EmptyState } from '@/components/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'
import { formatToman, formatDate, formatDateTime, parseDate } from '@/lib/utils'
import { formatJalali } from '@/lib/jalali'

const SOURCE_FA: Record<string, string> = {
  web: 'وب (خودم)',
  bot: 'بات من',
  sync: 'سینک مرزبان',
  delegate: 'نماینده',
}

function dateCell(value: string): string {
  const d = parseDate(value)
  if (Number.isNaN(d.getTime())) return '—'
  return `${formatDateTime(value)} · ${formatJalali(d).slice(0, 10)}`
}

function csvField(v: unknown): string {
  const s = v === null || v === undefined ? '' : String(v)
  return `"${s.replace(/"/g, '""')}"`
}

// ── Void dialog ──────────────────────────────────────────────────────────────
// Reason is mandatory (min 3 chars — "اشتباه بود" is a reason, "" is not).
// After posting, the response's before/after scope balances are shown so the
// operator sees in place what the void did to the books.
function VoidDialog({ entry, onClose }: { entry: LedgerEntry; onClose: () => void }) {
  const [reason, setReason] = React.useState('')
  const [result, setResult] = React.useState<{ before: number; after: number } | null>(null)
  const qc = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => ledgerApi.void(entry.id, reason.trim()),
    onSuccess: (res) => {
      setResult({ before: res.scope_balance_before, after: res.scope_balance_after })
      toast.success('ثبت باطل شد')
      qc.invalidateQueries({ queryKey: ['ledger'] })
      qc.invalidateQueries({ queryKey: ['reports'] })
      qc.invalidateQueries({ queryKey: ['history'] })
      qc.invalidateQueries({ queryKey: ['accounts'] })
      qc.invalidateQueries({ queryKey: ['customers'] })
      qc.invalidateQueries({ queryKey: ['groups'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })
  const amount = entry.amount.toLocaleString('en-US')
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-md" dir="rtl">
        <DialogHeader>
          <DialogTitle>باطل‌کردن {entry.type === 'credit' ? 'پرداخت' : 'بدهی'} — {amount} تومان</DialogTitle>
          <DialogDescription>
            ردیف پاک نمی‌شود؛ با «باطل‌شده» مُهر می‌شود و از همهٔ محاسبات پولی حذف می‌ماند. علت برای همیشه روی ردیف می‌ماند.
          </DialogDescription>
        </DialogHeader>
        {result ? (
          <div className="flex flex-col gap-2 text-sm">
            <div className="rounded-md border border-border bg-muted/40 p-3">
              <p className="text-xs text-muted-foreground">ترازِ همان حساب پیش و بعد:</p>
              <p className="mt-1 font-medium tabular-nums">
                {formatToman(result.before)} → {formatToman(result.after)}
              </p>
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={onClose}>بستن</Button>
            </DialogFooter>
          </div>
        ) : (
          <>
            <div className="flex flex-col gap-2">
              <Label htmlFor="void-reason">علت باطل‌کردن (الزامی)</Label>
              <Input
                id="void-reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="مثلاً: واریز تکراری — دوبار ثبت شده بود"
              />
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={onClose} disabled={mutation.isPending}>انصراف</Button>
              <Button
                variant="destructive"
                onClick={() => mutation.mutate()}
                disabled={mutation.isPending || reason.trim().length < 3}
              >
                {mutation.isPending ? 'در حال ثبت…' : 'باطل کن'}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

function UnvoidButton({ id }: { id: number }) {
  const qc = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => ledgerApi.unvoid(id),
    onSuccess: () => {
      toast.success('باطل‌شدن برداشته شد — ردیف دوباره در محاسبات است')
      qc.invalidateQueries({ queryKey: ['ledger'] })
      qc.invalidateQueries({ queryKey: ['reports'] })
      qc.invalidateQueries({ queryKey: ['history'] })
      qc.invalidateQueries({ queryKey: ['accounts'] })
      qc.invalidateQueries({ queryKey: ['customers'] })
      qc.invalidateQueries({ queryKey: ['groups'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })
  return (
    <Button
      size="sm"
      variant="ghost"
      className="h-7 gap-1 px-2 text-xs"
      disabled={mutation.isPending}
      onClick={(e) => {
        e.stopPropagation()
        mutation.mutate()
      }}
      title="برداشتن مُهر باطل — ردیف دوباره در همهٔ محاسبات حساب می‌شود"
    >
      <RotateCcw className="h-3 w-3" />
      بازگردانی
    </Button>
  )
}

// ── the page ─────────────────────────────────────────────────────────────────
export function LedgerPage() {
  React.useEffect(() => {
    document.title = 'Shiraze | دفتر کل'
  }, [])

  const [search, setSearch] = React.useState('')
  const [typeFilter, setTypeFilter] = React.useState<'all' | 'charge' | 'credit'>('all')
  const [sourceFilter, setSourceFilter] = React.useState<'all' | 'web' | 'bot' | 'sync' | 'delegate'>('all')
  const [includeVoided, setIncludeVoided] = React.useState(true)
  const [voidTarget, setVoidTarget] = React.useState<LedgerEntry | null>(null)

  const params = React.useMemo<Parameters<typeof ledgerApi.list>[0]>(() => {
    const p: Parameters<typeof ledgerApi.list>[0] = { limit: 1000 }
    if (typeFilter !== 'all') p.type = typeFilter
    if (sourceFilter !== 'all') p.source = sourceFilter
    if (search.trim()) p.q = search.trim()
    if (!includeVoided) p.include_voided = false
    return p
  }, [typeFilter, sourceFilter, search, includeVoided])

  const { data, isLoading } = useQuery({
    queryKey: ['ledger', 'book', params],
    queryFn: () => ledgerApi.list(params),
  })

  // Name maps — the ledger rows carry bare ids; the book page resolves them
  // client-side from the same cached lists the other pages already use.
  const accountsQuery = useQuery({ queryKey: ['history', 'accounts'], queryFn: () => historyApi.accounts() })
  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: () => customersApi.list() })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: () => groupsApi.list() })
  const accountName = React.useMemo(() => {
    const m = new Map<number, string>()
    for (const a of accountsQuery.data ?? []) m.set(a.id, a.username)
    return m
  }, [accountsQuery.data])
  const customerName = React.useMemo(() => {
    const m = new Map<number, string>()
    for (const c of customersQuery.data ?? []) m.set(c.id, c.name)
    return m
  }, [customersQuery.data])
  const groupName = React.useMemo(() => {
    const m = new Map<number, string>()
    for (const g of groupsQuery.data ?? []) m.set(g.id, g.name)
    return m
  }, [groupsQuery.data])

  function who(e: LedgerEntry): string {
    if (e.account_id !== null) return accountName.get(e.account_id) ?? `اکانت #${e.account_id}`
    if (e.group_id !== null) return groupName.get(e.group_id) ?? `گروه #${e.group_id}`
    if (e.customer_id !== null) return customerName.get(e.customer_id) ?? `مشتری #${e.customer_id}`
    return '—'
  }

  const rows = data ?? []
  const liveCharges = rows.filter((r) => !r.voided_at && r.type === 'charge').reduce((s, r) => s + r.amount, 0)
  const liveCredits = rows.filter((r) => !r.voided_at && r.type === 'credit').reduce((s, r) => s + r.amount, 0)
  const voidedCount = rows.filter((r) => r.voided_at).length

  function exportCsv() {
    const header = ['id', 'date', 'jalali', 'who', 'type', 'amount_toman', 'gb', 'source', 'operator', 'voided', 'void_reason', 'note']
    const lines = [header.join(',')].concat(
      rows.map((e) =>
        [
          e.id,
          formatDate(e.date),
          formatJalali(parseDate(e.date)),
          who(e),
          e.type === 'charge' ? 'بدهی' : 'پرداخت',
          e.amount,
          e.gb_amount ?? '',
          SOURCE_FA[e.source] ?? e.source,
          e.created_by ?? '',
          e.voided_at ? 'بله' : 'خیر',
          e.void_reason ?? '',
          e.note ?? '',
        ].map(csvField).join(','),
      ),
    )
    // BOM so Excel reads the Persian notes correctly (same as the history CSV).
    const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `ledger-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">دفتر کل</h1>
          <p className="text-xs text-muted-foreground">
            همهٔ پول‌های ثبت‌شده در یک جدول. ردیف اشتباه را «باطل» کن: از همهٔ محاسبات حذف می‌شود ولی با علتش دیده می‌شود.
          </p>
        </div>
        <Button size="sm" variant="outline" onClick={exportCsv} disabled={rows.length === 0} className="gap-1.5">
          <Download className="h-3.5 w-3.5" />
          CSV ({rows.length})
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="text-[11px] text-muted-foreground">جمع بدهی‌های زندهٔ این لیست</p>
          <p className="mt-0.5 text-sm font-semibold tabular-nums">{formatToman(liveCharges)}</p>
        </div>
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="text-[11px] text-muted-foreground">جمع پرداخت‌های زندهٔ این لیست</p>
          <p className="mt-0.5 text-sm font-semibold tabular-nums">{formatToman(liveCredits)}</p>
        </div>
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="text-[11px] text-muted-foreground">باطل‌شده در این لیست</p>
          <p className="mt-0.5 text-sm font-semibold tabular-nums">{voidedCount}</p>
        </div>
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="text-[11px] text-muted-foreground">تعداد ردیف (سقف ۱۰۰۰)</p>
          <p className="mt-0.5 text-sm font-semibold tabular-nums">{rows.length}</p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-muted-foreground" />
          <Input placeholder="جستجو در توضیحات…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-8" />
        </div>
        <Select value={typeFilter} onValueChange={(v) => setTypeFilter(v as typeof typeFilter)}>
          <SelectTrigger className="w-[130px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">همهٔ انواع</SelectItem>
            <SelectItem value="charge">بدهی</SelectItem>
            <SelectItem value="credit">پرداخت</SelectItem>
          </SelectContent>
        </Select>
        <Select value={sourceFilter} onValueChange={(v) => setSourceFilter(v as typeof sourceFilter)}>
          <SelectTrigger className="w-[150px]"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">همهٔ منابع</SelectItem>
            {Object.entries(SOURCE_FA).map(([k, v]) => (
              <SelectItem key={k} value={k}>{v}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          type="button"
          size="sm"
          variant={includeVoided ? 'default' : 'outline'}
          aria-pressed={includeVoided}
          onClick={() => setIncludeVoided((v) => !v)}
        >
          باطل‌شده‌ها {includeVoided ? 'دیده شوند' : 'پنهان شوند'}
        </Button>
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>زمان</TableHead>
              <TableHead>حساب</TableHead>
              <TableHead>نوع</TableHead>
              <TableHead className="text-right">مبلغ</TableHead>
              <TableHead className="hidden text-right md:table-cell">گیگابایت</TableHead>
              <TableHead className="hidden lg:table-cell">منبع</TableHead>
              <TableHead className="hidden lg:table-cell">توضیح</TableHead>
              <TableHead className="text-left">وضعیت</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              Array.from({ length: 8 }).map((_, i) => (
                <TableRow key={i}>
                  <TableCell><Skeleton className="h-4 w-36" /></TableCell>
                  <TableCell><Skeleton className="h-4 w-28" /></TableCell>
                  <TableCell><Skeleton className="h-5 w-14" /></TableCell>
                  <TableCell className="text-right"><Skeleton className="ml-auto h-4 w-20" /></TableCell>
                  <TableCell className="hidden text-right md:table-cell"><Skeleton className="ml-auto h-4 w-12" /></TableCell>
                  <TableCell className="hidden lg:table-cell"><Skeleton className="h-5 w-16" /></TableCell>
                  <TableCell className="hidden lg:table-cell"><Skeleton className="h-4 w-40" /></TableCell>
                  <TableCell><Skeleton className="h-4 w-16" /></TableCell>
                </TableRow>
              ))
            )}
            {!isLoading && rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={8} className="py-8">
                  <EmptyState title="ردیفی با این فیلترها پیدا نشد." />
                </TableCell>
              </TableRow>
            )}
            {rows.map((e) => {
              const voided = e.voided_at !== null
              return (
                <TableRow key={e.id} className={voided ? 'opacity-60' : undefined}>
                  <TableCell className="whitespace-nowrap text-xs text-muted-foreground">{dateCell(e.date)}</TableCell>
                  <TableCell className="max-w-[180px] truncate font-medium">{who(e)}</TableCell>
                  <TableCell>
                    <Badge variant={e.type === 'charge' ? 'destructive' : 'success'} className="text-[11px]">
                      {e.type === 'charge' ? 'بدهی' : 'پرداخت'}
                    </Badge>
                  </TableCell>
                  <TableCell className={`whitespace-nowrap text-right tabular-nums ${voided ? 'line-through' : ''}`}>
                    <span dir="ltr">{e.type === 'charge' ? '' : '−'}{formatToman(e.amount)}</span>
                  </TableCell>
                  <TableCell className="hidden text-right text-xs tabular-nums md:table-cell">
                    {e.gb_amount !== null ? <span dir="ltr">{e.gb_amount} GB</span> : '—'}
                  </TableCell>
                  <TableCell className="hidden lg:table-cell">
                    <Badge variant="outline" className="text-[11px]">{SOURCE_FA[e.source] ?? e.source}</Badge>
                    {e.created_by && <span className="ml-1 text-[11px] text-muted-foreground">{e.created_by}</span>}
                  </TableCell>
                  <TableCell className="hidden max-w-[220px] truncate text-xs text-muted-foreground lg:table-cell">{e.note ?? '—'}</TableCell>
                  <TableCell className="text-left">
                    {voided ? (
                      <div className="flex flex-col items-start gap-0.5">
                        <span className="inline-flex items-center gap-1 text-[11px] font-medium text-destructive">
                          <CircleSlash2 className="h-3 w-3" />
                          باطل‌شده
                        </span>
                        <span className="max-w-[160px] truncate text-[10px] text-muted-foreground" title={e.void_reason ?? ''}>
                          {e.void_reason} · {e.voided_by}
                        </span>
                        <UnvoidButton id={e.id} />
                      </div>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 gap-1 px-2 text-xs text-muted-foreground hover:text-destructive"
                        onClick={() => setVoidTarget(e)}
                      >
                        <Ban className="h-3 w-3" />
                        باطل کن
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      {voidTarget && <VoidDialog entry={voidTarget} onClose={() => setVoidTarget(null)} />}
    </div>
  )
}
