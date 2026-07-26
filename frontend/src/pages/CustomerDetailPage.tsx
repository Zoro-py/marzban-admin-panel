import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Building2 } from 'lucide-react'
import { customersApi, ledgerApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { LedgerActionDialog } from '@/components/ledger/LedgerActionDialog'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { UsageBar } from '@/components/UsageBar'
import { Money } from '@/components/Money'
import { StatusDot } from '@/components/StatusDot'
import { cn, daysUntil, formatDate } from '@/lib/utils'

export function CustomerDetailPage() {
  const { id } = useParams<{ id: string }>()
  const customerId = Number(id)
  const openAccount = useOpenAccountInspector()

  const customerQuery = useQuery({ queryKey: ['customers', customerId], queryFn: () => customersApi.get(customerId) })
  const accountsQuery = useQuery({ queryKey: ['accounts', { customerId }], queryFn: () => customersApi.accounts(customerId) })
  const ledgerQuery = useQuery({ queryKey: ['ledger', { customerId }], queryFn: () => ledgerApi.list({ customer_id: customerId }) })

  if (customerQuery.isLoading || !customerQuery.data) {
    return <p className="text-xs text-muted-foreground">Loading…</p>
  }

  const customer = customerQuery.data

  return (
    <div className="flex flex-col gap-4">
      <Link to="/customers" className="flex w-fit items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> Customers
      </Link>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">{customer.name}</h1>
          <p className="text-xs text-muted-foreground">{customer.contact ?? 'No contact info'}</p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div className="text-right">
            <p className="text-[11px] uppercase tracking-wider text-muted-foreground">Owes now</p>
            <Money amount={customer.net_owed} zero="settled" className="text-sm" />
          </div>
          {/* When this customer pays for exactly one account, attribute the
              entry to it — otherwise the money sits at customer level while
              the account row still reads as owing. With several accounts
              there's no honest way to guess the split, so it stays
              customer-level and only the header figure moves. */}
          <LedgerActionDialog
            customerId={customerId}
            accountId={accountsQuery.data?.length === 1 ? accountsQuery.data[0].id : undefined}
            currentBalance={customer.net_owed}
          />
          <NewAccountDialog defaultCustomerId={customerId} />
        </div>
      </div>

      {customer.represented_group_names.length > 0 && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-2.5 text-xs">
          <Building2 className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-muted-foreground">Represents:</span>
          {customer.represented_group_names.map((name) => (
            <Badge key={name} variant="secondary">
              {name}
            </Badge>
          ))}
          <Link to="/groups" className="ml-auto text-muted-foreground hover:text-foreground hover:underline">
            open groups →
          </Link>
        </div>
      )}

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <h2 className="text-[13px] font-semibold">
            Accounts owned directly
            {customer.represented_group_names.length > 0 && (
              <span className="ml-1.5 text-xs font-normal text-muted-foreground">— group members not counted here</span>
            )}
          </h2>
          <span className="text-xs tabular-nums text-muted-foreground">{accountsQuery.data?.length ?? 0}</span>
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Account</TableHead>
              <TableHead className="hidden md:table-cell">Role</TableHead>
              <TableHead>Usage</TableHead>
              <TableHead className="hidden sm:table-cell">Expires</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {accountsQuery.data?.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                  No accounts yet.
                </TableCell>
              </TableRow>
            )}
            {accountsQuery.data?.map((a) => {
              const days = daysUntil(a.expire)
              return (
                <TableRow key={a.id} className="cursor-pointer" onClick={() => openAccount(a.id)}>
                  <TableCell>
                    <span className="flex items-center gap-2">
                      <StatusDot status={a.status} />
                      <span className="font-mono text-xs font-medium">{a.marzban_username}</span>
                    </span>
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <span className="text-xs text-muted-foreground">{a.role === 'primary' ? 'primary' : 'sub-account'}</span>
                  </TableCell>
                  <TableCell>
                    <UsageBar used={a.used_traffic} limit={a.data_limit} compact />
                  </TableCell>
                  <TableCell className="hidden sm:table-cell">
                    {a.expire === null ? (
                      <span className="text-xs text-muted-foreground">never</span>
                    ) : (
                      <span
                        className={cn(
                          'text-xs tabular-nums',
                          days !== null && days < 0 && 'font-medium text-destructive',
                          days !== null && days >= 0 && days <= 3 && 'font-medium text-warning',
                        )}
                        title={formatDate(a.expire)}
                      >
                        {days! < 0 ? `${Math.abs(days!)}d ago` : `${days}d`}
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <h2 className="text-[13px] font-semibold">Transactions</h2>
          <span className="text-xs tabular-nums text-muted-foreground">{ledgerQuery.data?.length ?? 0}</span>
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Date</TableHead>
              <TableHead>Type</TableHead>
              <TableHead className="hidden md:table-cell">Note</TableHead>
              <TableHead className="text-right">Amount</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {ledgerQuery.data?.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="py-6 text-center text-muted-foreground">
                  No transactions yet.
                </TableCell>
              </TableRow>
            )}
            {ledgerQuery.data?.map((entry) => (
              <TableRow key={entry.id}>
                <TableCell className="text-xs text-muted-foreground">{formatDate(entry.date)}</TableCell>
                <TableCell>
                  <Badge variant={entry.type === 'charge' ? 'destructive' : 'success'}>
                    {entry.type === 'charge' ? 'debt' : 'payment'}
                  </Badge>
                </TableCell>
                <TableCell className="hidden max-w-[320px] truncate text-muted-foreground md:table-cell" title={entry.note ?? undefined}>
                  {entry.note ?? '—'}
                </TableCell>
                <TableCell className="text-right">
                  <span
                    className={cn(
                      'text-xs font-medium tabular-nums',
                      entry.type === 'charge' ? 'text-destructive' : 'text-success',
                    )}
                  >
                    {entry.type === 'charge' ? '+' : '−'}
                    {Math.round(entry.amount).toLocaleString('en-US')} T
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
