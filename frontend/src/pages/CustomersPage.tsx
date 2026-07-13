import * as React from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { customersApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { NewCustomerDialog } from '@/components/customers/NewCustomerDialog'
import { formatToman } from '@/lib/utils'
import { Search } from 'lucide-react'

export function CustomersPage() {
  const { data, isLoading } = useQuery({ queryKey: ['customers'], queryFn: customersApi.list })
  const [search, setSearch] = React.useState('')

  const filtered = React.useMemo(() => {
    if (!data) return []
    const q = search.trim().toLowerCase()
    if (!q) return data
    return data.filter((c) => c.name.toLowerCase().includes(q) || c.contact?.toLowerCase().includes(q))
  }, [data, search])

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Customers</h1>
          <p className="text-sm text-muted-foreground">Billing contacts — each may own several Marzban accounts.</p>
        </div>
        <NewCustomerDialog />
      </div>

      <div className="relative max-w-xs">
        <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input placeholder="Search customers…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-8" />
      </div>

      <div className="rounded-xl border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Contact</TableHead>
              <TableHead>Accounts</TableHead>
              <TableHead className="text-right">Balance</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={4} className="text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {!isLoading && filtered.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="text-center text-muted-foreground">
                  No customers yet.
                </TableCell>
              </TableRow>
            )}
            {filtered.map((c) => (
              <TableRow key={c.id} className="cursor-pointer">
                <TableCell>
                  <Link to={`/customers/${c.id}`} className="font-medium hover:underline">
                    {c.name}
                  </Link>
                  {c.is_group_rep && (
                    <Badge variant="outline" className="ml-2">
                      group rep
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="text-muted-foreground">{c.contact ?? '—'}</TableCell>
                <TableCell>{c.account_count}</TableCell>
                <TableCell className="text-right">
                  {c.balance === 0 ? (
                    <Badge variant="success">settled</Badge>
                  ) : c.balance > 0 ? (
                    <Badge variant="destructive">{formatToman(c.balance)} owed</Badge>
                  ) : (
                    <Badge variant="secondary">{formatToman(Math.abs(c.balance))} credit</Badge>
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
