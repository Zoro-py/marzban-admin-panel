import * as React from 'react'
import { DndContext, DragOverlay, useDraggable, useDroppable, type DragEndEvent, type DragStartEvent } from '@dnd-kit/core'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, apiErrorMessage } from '@/lib/api'
import type { Account, GroupWithBalance } from '@/lib/types'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { UsageBar } from '@/components/UsageBar'
import { cn } from '@/lib/utils'
import { GripVertical, Users } from 'lucide-react'

const UNGROUPED = '__ungrouped__'

interface AccountsBoardProps {
  accounts: Account[]
  groups: GroupWithBalance[]
  isLoading: boolean
}

function AccountCard({ account, dragging = false }: { account: Account; dragging?: boolean }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: account.id,
    data: account,
  })

  return (
    <div
      ref={setNodeRef}
      {...listeners}
      {...attributes}
      style={
        transform && !dragging
          ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)`, zIndex: 10 }
          : undefined
      }
      className={cn(
        'flex cursor-grab items-center gap-2 rounded-lg border border-border bg-card p-2.5 text-sm shadow-sm active:cursor-grabbing',
        (isDragging || dragging) && 'opacity-90 shadow-lg ring-2 ring-primary',
      )}
    >
      <GripVertical className="h-4 w-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <p className="truncate font-mono text-xs font-medium">{account.marzban_username}</p>
        <UsageBar used={account.used_traffic} limit={account.data_limit} compact className="mt-1" />
      </div>
    </div>
  )
}

function Column({
  id,
  title,
  subtitle,
  accounts,
}: {
  id: string
  title: string
  subtitle?: string
  accounts: Account[]
}) {
  const { setNodeRef, isOver } = useDroppable({ id })

  return (
    <Card ref={setNodeRef} className={cn('flex w-72 shrink-0 flex-col transition-colors', isOver && 'ring-2 ring-primary')}>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-1.5">
            <Users className="h-3.5 w-3.5 text-muted-foreground" />
            {title}
          </span>
          <Badge variant="outline">{accounts.length}</Badge>
        </CardTitle>
        {subtitle && <p className="text-xs text-muted-foreground">{subtitle}</p>}
      </CardHeader>
      <CardContent className="flex min-h-24 flex-1 flex-col gap-2 pt-0">
        {accounts.length === 0 && <p className="rounded-lg border border-dashed border-border p-3 text-center text-xs text-muted-foreground">Drop here</p>}
        {accounts.map((a) => (
          <AccountCard key={a.id} account={a} />
        ))}
      </CardContent>
    </Card>
  )
}

export function AccountsBoard({ accounts, groups, isLoading }: AccountsBoardProps) {
  const [activeAccount, setActiveAccount] = React.useState<Account | null>(null)
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: ({ accountId, groupId }: { accountId: number; groupId: number | null }) =>
      accountsApi.updateRelationship(accountId, { group_id: groupId }),
    onSuccess: (updated) => {
      toast.success(`${updated.marzban_username} moved`)
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  function handleDragStart(event: DragStartEvent) {
    setActiveAccount((event.active.data.current as Account) ?? null)
  }

  function handleDragEnd(event: DragEndEvent) {
    setActiveAccount(null)
    const { active, over } = event
    if (!over) return
    const account = active.data.current as Account
    const targetGroupId = over.id === UNGROUPED ? null : Number(over.id)
    if (account.group_id === targetGroupId) return
    mutation.mutate({ accountId: account.id, groupId: targetGroupId })
  }

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>
  }

  const byGroup = new Map<number, Account[]>()
  const ungrouped: Account[] = []
  for (const a of accounts) {
    if (a.group_id) {
      byGroup.set(a.group_id, [...(byGroup.get(a.group_id) ?? []), a])
    } else {
      ungrouped.push(a)
    }
  }

  return (
    <DndContext onDragStart={handleDragStart} onDragEnd={handleDragEnd}>
      <p className="mb-3 text-xs text-muted-foreground">Drag a card into a group to assign it — or back out to ungroup.</p>
      <div className="flex gap-4 overflow-x-auto pb-2">
        <Column id={UNGROUPED} title="Ungrouped" accounts={ungrouped} />
        {groups.map((g) => (
          <Column
            key={g.id}
            id={String(g.id)}
            title={g.name}
            subtitle={g.rate_per_gb ? `${g.rate_per_gb.toLocaleString()} T/GB` : undefined}
            accounts={byGroup.get(g.id) ?? []}
          />
        ))}
      </div>
      <DragOverlay>{activeAccount ? <AccountCard account={activeAccount} dragging /> : null}</DragOverlay>
    </DndContext>
  )
}
