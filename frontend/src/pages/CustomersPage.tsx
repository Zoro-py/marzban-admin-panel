import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { customersApi } from '@/lib/api'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { NewCustomerDialog } from '@/components/customers/NewCustomerDialog'
import { Money } from '@/components/Money'
import { Search } from 'lucide-react'

export function CustomersPage() {
  const { data, isLoading } = useQuery({ queryKey: ['customers'], queryFn: customersApi.list })
  const [search, setSearch] = React.useState('')
  const navigate = useNavigate()

  const filtered = React.useMemo(() => {
    if (!data) return []
    const q = search.trim().toLowerCase()
    if (!q) return data
    return data.filter((c) => c.name.toLowerCase().includes(q) || c.contact?.toLowerCase().includes(q))
  }, [data, search])

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Customers</h1>
          <p className="text-xs text-muted-foreground">
            Billing contacts — they own accounts directly, represent a group's invoice, or both.
          </p>
        </div>
        <NewCustomerDialog />
      </div>

      <div className="relative max-w-xs">
        <Search className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-muted-foreground" />
        <Input placeholder="Filter customers…" value={search} onChange={(e) => setSearch(e.target.value)} className="pl-8" />
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Contact</TableHead>
              <TableHead>Represents</TableHead>
              <TableHead className="text-right">Accounts</TableHead>
              <TableHead className="text-right">Balance</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {!isLoading && filtered.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="py-8 text-center text-muted-foreground">
                  No customers yet.
                </TableCell>
              </TableRow>
            )}
            {filtered.map((c) => (
              <TableRow key={c.id} className="cursor-pointer" onClick={() => navigate(`/customers/${c.id}`)}>
                <TableCell className="font-medium">{c.name}</TableCell>
                <TableCell className="text-muted-foreground">{c.contact ?? '—'}</TableCell>
                <TableCell>
                  {c.represented_group_names.length > 0 ? (
                    <span className="flex flex-wrap gap-1">
                      {c.represented_group_names.map((name) => (
                        <Badge key={name} variant="secondary">
                          {name}
                        </Badge>
                      ))}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </TableCell>
                <TableCell className="text-right tabular-nums">{c.account_count}</TableCell>
                <TableCell className="text-right">
                  <Money amount={c.balance} zero="settled" className="text-xs" />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
