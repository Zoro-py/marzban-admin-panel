import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { accountsApi, customersApi, groupsApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { SearchableSelect } from '@/components/ui/searchable-select'
import { FolderInput } from 'lucide-react'
import type { AccountRow } from '@/lib/types'

// Bulk ownership move for the accounts the operator ticked on the board.
// One dialog, one target, one confirm — moving N accounts used to mean
// opening the inspector N times (4 taps + a save each); now it is tick,
// tick, Move, pick, confirm. The writes go through the SAME per-account
// relationship endpoint the inspector uses, so every guard (D21 mode-flip
// baseline handling, the shop-link payg block, customer/group existence)
// runs per account, and a refused account is reported by name instead of
// silently skipped or half-rolled-back.
//
// "True move" semantics: moving to a customer clears the group field and
// vice versa — leaving both set would keep the account billed by a group it
// was supposedly moved out of.
const NONE = '__none__'

export function BulkMoveBar({ selected, onClear }: { selected: AccountRow[]; onClear: () => void }) {
  const [open, setOpen] = React.useState(false)
  const [target, setTarget] = React.useState(NONE)
  const queryClient = useQueryClient()

  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: () => customersApi.list() })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: () => groupsApi.list() })

  const customers = customersQuery.data ?? []
  const groups = groupsQuery.data ?? []
  const targetKind: 'customer' | 'group' | null = target.startsWith('c:')
    ? 'customer'
    : target.startsWith('g:')
      ? 'group'
      : null
  const targetId = targetKind ? Number(target.slice(2)) : null
  const targetLabel =
    targetKind === 'customer'
      ? customers.find((c) => c.id === targetId)?.name
      : targetKind === 'group'
        ? groups.find((g) => g.id === targetId)?.name
        : undefined

  const mutation = useMutation({
    mutationFn: async () => {
      const body =
        targetKind === 'customer' ? { customer_id: targetId, group_id: null } : { customer_id: null, group_id: targetId }
      const failed: { username: string; error: string }[] = []
      let moved = 0
      for (const a of selected) {
        try {
          await accountsApi.updateRelationship(a.id, body)
          moved += 1
        } catch (err) {
          failed.push({ username: a.marzban_username, error: apiErrorMessage(err) })
        }
      }
      return { moved, failed }
    },
    onSuccess: ({ moved, failed }) => {
      if (failed.length === 0) {
        toast.success(`Moved ${moved} account${moved === 1 ? '' : 's'} to ${targetLabel}`)
      } else {
        toast.warning(
          `Moved ${moved} of ${moved + failed.length} — refused: ` +
            failed.map((f) => `${f.username} (${f.error})`).join(' · '),
          { duration: 12000 },
        )
      }
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      onClear()
      setOpen(false)
      setTarget(NONE)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        className="gap-1.5"
        onClick={() => setOpen(true)}
        disabled={mutation.isPending}
      >
        <FolderInput className="h-3.5 w-3.5" /> Move
      </Button>
      <span className="mx-1 h-4 w-px bg-border" aria-hidden />

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Move {selected.length} account{selected.length === 1 ? '' : 's'}</DialogTitle>
            <DialogDescription>
              {selected.length <= 5
                ? selected.map((a) => a.marzban_username).join(' · ')
                : `${selected.slice(0, 5).map((a) => a.marzban_username).join(' · ')} … +${selected.length - 5} more`}
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col gap-2.5">
            <div className="flex flex-col gap-1.5">
              <Label className="text-xs">Move to</Label>
              <SearchableSelect
                value={target}
                onValueChange={setTarget}
                placeholder="Pick a customer or group…"
                searchPlaceholder="Search customers and groups…"
                options={[
                  ...customers.map((c) => ({ value: `c:${c.id}`, label: `Customer: ${c.name}` })),
                  ...groups.map((g) => ({ value: `g:${g.id}`, label: `Group: ${g.name}` })),
                ]}
              />
            </div>
            {targetKind && (
              <p className="text-xs text-muted-foreground">
                {targetKind === 'group'
                  ? `Billing follows the group's mode from now on — the guarded endpoint rolls baselines on any mode flip.`
                  : `The account leaves its group and bills standalone under this customer.`}
              </p>
            )}
          </div>

          <DialogFooter>
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" disabled={!targetKind || mutation.isPending} onClick={() => mutation.mutate()}>
              {mutation.isPending ? 'Moving…' : `Move ${selected.length} to ${targetLabel ?? '…'}`}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
