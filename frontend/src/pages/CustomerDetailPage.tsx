import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Building2 } from 'lucide-react'
import { customersApi, groupsApi, ledgerApi } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { LedgerActionDialog } from '@/components/ledger/LedgerActionDialog'
import { NewAccountDialog } from '@/components/accounts/NewAccountDialog'
import { AdjustAccountDialog } from '@/components/accounts/AdjustAccountDialog'
import { RelationshipDialog } from '@/components/accounts/RelationshipDialog'
import { ResetUsageDialog } from '@/components/accounts/ResetUsageDialog'
import { BillingDialog } from '@/components/accounts/BillingDialog'
import { UsageBar } from '@/components/UsageBar'
import { formatDate, formatToman } from '@/lib/utils'

export function CustomerDetailPage() {
  const { id } = useParams<{ id: string }>()
  const customerId = Number(id)

  const customerQuery = useQuery({ queryKey: ['customers', customerId], queryFn: () => customersApi.get(customerId) })
  const accountsQuery = useQuery({ queryKey: ['accounts', { customerId }], queryFn: () => customersApi.accounts(customerId) })
  const ledgerQuery = useQuery({ queryKey: ['ledger', { customerId }], queryFn: () => ledgerApi.list({ customer_id: customerId }) })
  // No dedicated endpoint for "groups this customer represents" — the group
  // list is small enough to fetch and filter client-side, same pattern used
  // elsewhere in this app. This is specifically what makes a customer with
  // group-only accounts NOT read as "0 accounts, nothing here" — see item 5.
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list })
  const representedGroups = groupsQuery.data?.filter((g) => g.representative_customer_id === customerId) ?? []

  if (customerQuery.isLoading || !customerQuery.data) {
    return <p className="text-sm text-muted-foreground">Loading…</p>
  }

  const customer = customerQuery.data

  return (
    <div className="flex flex-col gap-6">
      <Link to="/customers" className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> Back to customers
      </Link>

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{customer.name}</h1>
          <p className="text-sm text-muted-foreground">{customer.contact ?? 'No contact info'}</p>
        </div>
        <div className="flex items-center gap-2">
          {customer.balance === 0 ? (
            <Badge variant="success">settled</Badge>
          ) : customer.balance > 0 ? (
            <Badge variant="destructive">{formatToman(customer.balance)} owed</Badge>
          ) : (
            <Badge variant="secondary">{formatToman(Math.abs(customer.balance))} credit</Badge>
          )}
          <LedgerActionDialog customerId={customerId} currentBalance={customer.balance} />
          <NewAccountDialog defaultCustomerId={customerId} />
        </div>
      </div>

      {representedGroups.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Building2 className="h-4 w-4 text-muted-foreground" /> Represents {representedGroups.length} group
              {representedGroups.length > 1 ? 's' : ''}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {representedGroups.map((g) => (
              <Link
                key={g.id}
                to={`/groups/${g.id}`}
                className="flex items-center justify-between rounded-md px-2 py-1.5 text-sm hover:bg-accent"
              >
                <span className="font-medium">{g.name}</span>
                <span className="text-muted-foreground">{g.account_count} accounts</span>
              </Link>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Accounts owned directly ({accountsQuery.data?.length ?? 0})
            {representedGroups.length > 0 && (
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                — not counting group members above
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Username</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Usage</TableHead>
                <TableHead>Expires</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accountsQuery.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-center text-muted-foreground">
                    No accounts yet.
                  </TableCell>
                </TableRow>
              )}
              {accountsQuery.data?.map((a) => (
                <TableRow key={a.id}>
                  <TableCell className="font-mono">
                    <Link to={`/accounts?highlight=${a.id}`} className="hover:underline">
                      {a.marzban_username}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline">{a.role}</Badge>
                  </TableCell>
                  <TableCell>
                    <UsageBar used={a.used_traffic} limit={a.data_limit} />
                  </TableCell>
                  <TableCell>{formatDate(a.expire)}</TableCell>
                  <TableCell>
                    <Badge variant={a.status === 'active' ? 'success' : 'outline'}>{a.status ?? 'unknown'}</Badge>
                  </TableCell>
                  <TableCell className="flex flex-wrap justify-end gap-2">
                    <AdjustAccountDialog account={a} />
                    <ResetUsageDialog account={a} />
                    <BillingDialog account={a} />
                    <RelationshipDialog account={a} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Transaction history</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Date</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Note</TableHead>
                <TableHead className="text-right">Amount</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ledgerQuery.data?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={4} className="text-center text-muted-foreground">
                    No transactions yet.
                  </TableCell>
                </TableRow>
              )}
              {ledgerQuery.data?.map((entry) => (
                <TableRow key={entry.id}>
                  <TableCell>{formatDate(entry.date)}</TableCell>
                  <TableCell>
                    <Badge variant={entry.type === 'charge' ? 'destructive' : 'success'}>
                      {entry.type === 'charge' ? 'debt' : 'credit'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{entry.note ?? '—'}</TableCell>
                  <TableCell className="text-right tabular-nums">{formatToman(entry.amount)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
