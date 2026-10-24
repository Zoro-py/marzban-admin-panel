import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Bot, Clock, Link2, MessageCircle, Search, UserCog } from 'lucide-react'
import { accountsApi, connectionsApi } from '@/lib/api'
import type { BotHistoryItem, ConnectionRow } from '@/lib/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { EmptyState } from '@/components/EmptyState'
import { Skeleton } from '@/components/ui/skeleton'
import { formatToman } from '@/lib/utils'
import { formatJalali } from '@/lib/jalali'
import { parseDate } from '@/lib/utils'

function fmtDate(value: string | null | undefined): string {
  if (!value) return '—'
  const d = parseDate(value)
  if (Number.isNaN(d.getTime())) return '—'
  return formatJalali(d)
}

function fmtAgo(value: string | null | undefined): string {
  if (!value) return '—'
  const d = parseDate(value)
  if (Number.isNaN(d.getTime())) return '—'
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000)
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} دقیقه پیش`
  if (s < 86400) return `${Math.round(s / 3600)} ساعت پیش`
  return `${Math.round(s / 86400)} روز پیش`
}

const KIND_STYLE: Record<BotHistoryItem['kind'], { label: string; cls: string }> = {
  order: { label: 'سفارش', cls: 'border-primary/25 bg-primary/10 text-primary' },
  topup: { label: 'واریز', cls: 'border-success/25 bg-success/10 text-success' },
  wallet: { label: 'کیف پول', cls: 'border-credit/25 bg-credit/10 text-credit' },
  account_event: { label: 'اکانت', cls: 'border-border bg-muted text-foreground' },
  ledger: { label: 'حساب‌کتاب', cls: 'border-warning/25 bg-warning/10 text-warning' },
}

// ── per-customer bot timeline dialog (R7) ────────────────────────────────────
function BotHistoryDialog({ row, onClose }: { row: ConnectionRow; onClose: () => void }) {
  const { data, isLoading } = useQuery({
    queryKey: ['connections', 'bot-history', row.customer_id],
    queryFn: () => connectionsApi.botHistory(row.customer_id),
  })
  const items = data ?? []
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-xl" dir="rtl">
        <DialogHeader>
          <DialogTitle>تاریخچهٔ بات — {row.name}</DialogTitle>
          <DialogDescription>
            هر چیزی که از راه تلگرام اتفاق افتاده: سفارش‌ها، واریزها، کیف پول، کار نماینده و کارهای بات خودت. از خود ردیف‌های موجود سرهم شده — چیزی اضافه ثبت نمی‌شود.
          </DialogDescription>
        </DialogHeader>
        {isLoading && <Skeleton className="h-40 w-full" />}
        {!isLoading && items.length === 0 && (
          <EmptyState title="هنوز هیچ رد باتی برای این مشتری نیست." />
        )}
        <ol className="flex flex-col gap-2">
          {items.map((it, i) => {
            const style = KIND_STYLE[it.kind]
            return (
              <li key={i} className="flex gap-3 rounded-md border border-border bg-card p-3">
                <div className="flex w-16 shrink-0 flex-col items-start gap-1">
                  <Badge variant="outline" className={`text-[10px] ${style.cls}`}>{style.label}</Badge>
                  <span className="text-[10px] text-muted-foreground">{fmtAgo(it.ts)}</span>
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{it.title}</p>
                  {it.detail && <p className="mt-0.5 break-words text-xs text-muted-foreground">{it.detail}</p>}
                  {it.account_username && (
                    <p className="mt-0.5 text-[11px] text-muted-foreground" dir="ltr">«{it.account_username}»</p>
                  )}
                </div>
                {it.amount !== null && it.amount !== undefined && (
                  <span className="shrink-0 text-xs tabular-nums" dir="ltr">{formatToman(it.amount)}</span>
                )}
              </li>
            )
          })}
        </ol>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>بستن</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ── the page ─────────────────────────────────────────────────────────────────
export function ConnectionsPage() {
  React.useEffect(() => {
    document.title = 'Shiraze | اتصال تلگرام'
  }, [])
  const { data, isLoading } = useQuery({
    queryKey: ['connections'],
    queryFn: () => connectionsApi.list(),
  })
  const [search, setSearch] = React.useState('')
  const [tab, setTab] = React.useState<'all' | 'linked' | 'pending' | 'none'>('all')
  const [historyRow, setHistoryRow] = React.useState<ConnectionRow | null>(null)

  // R4 (owner feedback): "to connect account X I have to find its owner
  // first" — so the search here matches ACCOUNT USERNAMES too, and each row
  // names the accounts that matched. One query, cached with the palette key.
  const accountsQuery = useQuery({ queryKey: ['accounts', 'palette'], queryFn: () => accountsApi.list() })
  const usernamesByCustomer = React.useMemo(() => {
    const m = new Map<number, string[]>()
    for (const a of accountsQuery.data ?? []) {
      if (a.customer_id === null) continue
      const list = m.get(a.customer_id) ?? []
      list.push(a.marzban_username)
      m.set(a.customer_id, list)
    }
    return m
  }, [accountsQuery.data])

  const rows = data ?? []
  const linkedCount = rows.filter((r) => r.shop.state === 'linked').length
  const pendingCount = rows.filter((r) => r.shop.state === 'pending').length
  const delegateCount = rows.filter((r) => r.delegate?.state === 'claimed').length

  // Plain filter, no useMemo: `rows` is `data ?? []`, a fresh array each
  // render — memoizing over it warns (exhaustive-deps) and saves nothing on
  // a per-customer list this size.
  const filtered = (() => {
    const q = search.trim().toLowerCase()
    let out = rows
    if (tab === 'linked') out = out.filter((r) => r.shop.state === 'linked')
    if (tab === 'pending') out = out.filter((r) => r.shop.state === 'pending' || r.shop.bind_claim_username || r.shop.bind_claim_display_name)
    if (tab === 'none') out = out.filter((r) => r.shop.state === 'none')
    if (!q) return out
    return out.filter(
      (r) =>
        r.name.toLowerCase().includes(q) ||
        r.contact?.toLowerCase().includes(q) ||
        (r.shop.telegram_username ?? '').toLowerCase().includes(q) ||
        String(r.shop.telegram_id ?? '').includes(q) ||
        (usernamesByCustomer.get(r.customer_id) ?? []).some((u) => u.toLowerCase().includes(q)),
    )
  })()

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight">اتصال تلگرام</h1>
        <p className="text-xs text-muted-foreground">
          چه کسی از راه تلگرام به سرویس‌هایش دسترسی دارد: لینک بات فروش (وقتی صاحبش استارت زده)، دعوت‌های معلق، و نماینده‌ها. روی «تاریخچه» بزنی کل کارِ باتیِ همان مشتری را می‌بینی.
        </p>
      </div>

      <div className="grid grid-cols-3 gap-2">
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground"><MessageCircle className="h-3.5 w-3.5" /> وصل‌شده به بات فروش</p>
          <p className="mt-0.5 text-sm font-semibold tabular-nums">{linkedCount}</p>
        </div>
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground"><Clock className="h-3.5 w-3.5" /> دعوت معلق</p>
          <p className="mt-0.5 text-sm font-semibold tabular-nums">{pendingCount}</p>
        </div>
        <div className="rounded-lg border border-border bg-card p-3">
          <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground"><UserCog className="h-3.5 w-3.5" /> نمایندهٔ فعال</p>
          <p className="mt-0.5 text-sm font-semibold tabular-nums">{delegateCount}</p>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="جستجو: نام، آیدی تلگرام، یوزرنیم…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8"
          />
        </div>
        {([['all', 'همه'], ['linked', 'وصل'], ['pending', 'دعوت معلق'], ['none', 'بدون اتصال']] as const).map(([k, label]) => (
          <Button
            key={k}
            type="button"
            size="sm"
            variant={tab === k ? 'default' : 'outline'}
            aria-pressed={tab === k}
            onClick={() => setTab(k)}
          >
            {label}
          </Button>
        ))}
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>مشتری</TableHead>
              <TableHead>بات فروش</TableHead>
              <TableHead className="hidden md:table-cell">کیف پول</TableHead>
              <TableHead>نماینده</TableHead>
              <TableHead className="hidden text-right sm:table-cell">اکانت‌ها</TableHead>
              <TableHead className="text-left">تاریخچه بات</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              Array.from({ length: 6 }).map((_, i) => (
                <TableRow key={i}>
                  <TableCell><Skeleton className="h-4 w-28" /></TableCell>
                  <TableCell><Skeleton className="h-4 w-36" /></TableCell>
                  <TableCell className="hidden md:table-cell"><Skeleton className="h-4 w-16" /></TableCell>
                  <TableCell><Skeleton className="h-4 w-20" /></TableCell>
                  <TableCell className="hidden text-right sm:table-cell"><Skeleton className="ml-auto h-4 w-8" /></TableCell>
                  <TableCell><Skeleton className="h-7 w-20" /></TableCell>
                </TableRow>
              ))
            )}
            {!isLoading && filtered.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="py-8">
                  <EmptyState title="مشتری‌ای با این فیلتر نیست." />
                </TableCell>
              </TableRow>
            )}
            {filtered.map((r) => (
              <TableRow key={r.customer_id}>
                <TableCell>
                  <Link to={`/customers/${r.customer_id}`} className="font-medium hover:underline">
                    {r.name}
                  </Link>
                  {r.kind === 'family' && <Badge variant="secondary" className="ml-1.5 text-[10px]">خانواده</Badge>}
                  {r.contact && <p className="text-[11px] text-muted-foreground">{r.contact}</p>}
                  {search.trim().length > 0 && (usernamesByCustomer.get(r.customer_id) ?? []).length > 0 && (
                    <p className="truncate text-[10px] text-muted-foreground/80" dir="ltr">
                      {(usernamesByCustomer.get(r.customer_id) ?? []).slice(0, 4).join(', ')}
                    </p>
                  )}
                </TableCell>
                <TableCell>
                  {r.shop.state === 'linked' && (
                    <div>
                      <p className="inline-flex items-center gap-1 text-xs font-medium text-success">
                        <Link2 className="h-3 w-3" /> وصل است
                      </p>
                      <p className="text-[11px] text-muted-foreground" dir="ltr">
                        {r.shop.display_name ?? r.shop.telegram_username ?? r.shop.telegram_id} · {r.shop.telegram_id}
                      </p>
                      <p className="text-[10px] text-muted-foreground">از {fmtDate(r.shop.linked_at)} · آخرین فعالیت {fmtAgo(r.shop.last_seen_at)}</p>
                    </div>
                  )}
                  {r.shop.state === 'pending' && (
                    <div>
                      <p className="inline-flex items-center gap-1 text-xs font-medium text-warning">
                        <Clock className="h-3 w-3" /> دعوت معلق
                      </p>
                      <p className="text-[10px] text-muted-foreground">مهلت: {fmtDate(r.shop.claim_expires_at)}</p>
                    </div>
                  )}
                  {r.shop.state === 'none' && !r.shop.bind_claim_username && !r.shop.bind_claim_display_name && (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                  {(r.shop.bind_claim_username || r.shop.bind_claim_display_name) && (
                    <div>
                      <p className="inline-flex items-center gap-1 text-xs font-medium text-warning">
                        <Clock className="h-3 w-3" /> منتظر اولین پیام
                      </p>
                      <p className="text-[11px] text-muted-foreground" dir="ltr">
                        {r.shop.bind_claim_username ? `@${r.shop.bind_claim_username}` : r.shop.bind_claim_display_name}
                      </p>
                    </div>
                  )}
                </TableCell>
                <TableCell className="hidden md:table-cell">
                  {r.shop.state === 'linked' && r.shop.wallet_balance !== null ? (
                    <span className="text-xs tabular-nums" dir="ltr">{formatToman(r.shop.wallet_balance)}</span>
                  ) : '—'}
                </TableCell>
                <TableCell>
                  {r.delegate ? (
                    <div>
                      <Badge
                        variant={r.delegate.state === 'claimed' ? 'success' : r.delegate.state === 'pending' ? 'warning' : 'secondary'}
                        className="text-[10px]"
                      >
                        {r.delegate.state === 'claimed' ? 'نمایندهٔ فعال' : r.delegate.state === 'pending' ? 'دعوت معلق' : 'لغوشده'}
                      </Badge>
                      {r.delegate.telegram_id && <p className="mt-0.5 text-[10px] text-muted-foreground" dir="ltr">tg {r.delegate.telegram_id}</p>}
                    </div>
                  ) : (
                    <span className="text-xs text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="hidden text-right tabular-nums sm:table-cell">{r.accounts_count}</TableCell>
                <TableCell className="text-left">
                  <Button size="sm" variant="outline" className="h-7 gap-1 px-2 text-xs" onClick={() => setHistoryRow(r)}>
                    <Bot className="h-3 w-3" />
                    تاریخچه
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        برای دعوت یا قطع اتصال، از کارت «بات فروش» در صفحهٔ خود مشتری استفاده کن — همان‌جا دعوت یک‌بارمصرف ساخته می‌شود، آیدی تلگرام مستقیم وصل می‌شود و اتصال برداشته می‌شود. مدیریت نماینده‌ها هم در صفحهٔ «نماینده‌ها»ست.
      </p>

      {historyRow && <BotHistoryDialog row={historyRow} onClose={() => setHistoryRow(null)} />}
    </div>
  )
}
