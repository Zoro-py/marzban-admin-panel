import * as React from 'react'
import { useQuery } from '@tanstack/react-query'
import { customersApi, groupsApi, accountsApi } from '@/lib/api'
import { SearchableSelect } from '@/components/ui/searchable-select'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { BalanceSinceControl } from '@/components/BalanceSinceControl'

type EntityKind = 'customer' | 'group' | 'account'

/** Pick ANY customer, group, or account — not just the one whose own page
 * you happen to be on — then a date, to see what they owe from that point
 * forward. Reuses BalanceSinceControl for the actual date-pick + balance
 * display once something is selected; this component's only job is turning
 * a kind + a searched name into the {customer_id|group_id|account_id} scope
 * that control already knows how to take. */
export function BalanceLookup() {
  const [kind, setKind] = React.useState<EntityKind>('customer')
  const [selectedId, setSelectedId] = React.useState('')

  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list, enabled: kind === 'customer' })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list, enabled: kind === 'group' })
  const accountsQuery = useQuery({ queryKey: ['accounts'], queryFn: () => accountsApi.list(), enabled: kind === 'account' })

  const isLoadingOptions =
    (kind === 'customer' && customersQuery.isLoading) ||
    (kind === 'group' && groupsQuery.isLoading) ||
    (kind === 'account' && accountsQuery.isLoading)

  const options = React.useMemo(() => {
    if (kind === 'customer') return (customersQuery.data ?? []).map((c) => ({ value: String(c.id), label: c.name }))
    if (kind === 'group') return (groupsQuery.data ?? []).map((g) => ({ value: String(g.id), label: g.name }))
    return (accountsQuery.data ?? []).map((a) => ({ value: String(a.id), label: a.marzban_username }))
  }, [kind, customersQuery.data, groupsQuery.data, accountsQuery.data])

  const scope = !selectedId
    ? null
    : kind === 'customer'
      ? ({ customer_id: Number(selectedId) } as const)
      : kind === 'group'
        ? ({ group_id: Number(selectedId) } as const)
        : ({ account_id: Number(selectedId) } as const)

  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card">
      <div className="border-b border-border px-4 py-2.5">
        <h2 className="text-[13px] font-semibold">Balance lookup</h2>
        <p className="text-xs text-muted-foreground">
          Pick any customer, group or account, then a date, to see what they owe from that point forward.
        </p>
      </div>
      <div className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-center gap-3">
          <Tabs
            value={kind}
            onValueChange={(v) => {
              setKind(v as EntityKind)
              setSelectedId('')
            }}
          >
            <TabsList>
              <TabsTrigger value="customer">Customer</TabsTrigger>
              <TabsTrigger value="group">Group</TabsTrigger>
              <TabsTrigger value="account">Account</TabsTrigger>
            </TabsList>
          </Tabs>
          <SearchableSelect
            value={selectedId}
            onValueChange={setSelectedId}
            options={options}
            placeholder={isLoadingOptions ? 'Loading…' : `Select a ${kind}…`}
            searchPlaceholder={`Search ${kind}s…`}
            disabled={isLoadingOptions}
          />
        </div>
        {scope && <BalanceSinceControl scope={scope} />}
      </div>
    </div>
  )
}
