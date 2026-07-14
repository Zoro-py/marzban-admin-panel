import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { groupsApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { formatToman } from '@/lib/utils'
import { RotateCcw } from 'lucide-react'

/** Item 3 of the follow-up feedback: settle always charges the computed
 * amount — there was no way to start a new cycle for a group that already
 * got paid some other way (cash, a manual "New debt/credit" entry recorded
 * separately) without either double-billing it or leaving stale usage
 * sitting in "pending" forever. This posts no ledger entry at all. */
export function ResetGroupCycleDialog({ groupId }: { groupId: number }) {
  const [open, setOpen] = React.useState(false)
  const queryClient = useQueryClient()

  const invoiceQuery = useQuery({
    queryKey: ['groups', groupId, 'invoice'],
    queryFn: () => groupsApi.invoice(groupId),
    enabled: open,
  })

  const resetMutation = useMutation({
    mutationFn: () => groupsApi.resetCycle(groupId),
    onSuccess: () => {
      toast.success('Cycle reset — no charge was posted')
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" className="gap-1.5">
          <RotateCcw className="h-4 w-4" /> Reset cycle
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Reset this cycle without charging</DialogTitle>
          <DialogDescription>
            Use this when the members already paid outside the ledger (cash, or a payment you recorded separately
            via "New debt/credit"). This starts a new cycle for all members — the usage below will NOT be posted as
            a charge.
          </DialogDescription>
        </DialogHeader>

        {invoiceQuery.isLoading && <p className="text-sm text-muted-foreground">Calculating…</p>}
        {invoiceQuery.data && (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Account</TableHead>
                  <TableHead className="text-right">Usage</TableHead>
                  <TableHead className="text-right">Value (not charged)</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {invoiceQuery.data.lines.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={3} className="text-center text-muted-foreground">
                      No usage yet this cycle.
                    </TableCell>
                  </TableRow>
                )}
                {invoiceQuery.data.lines.map((line) => (
                  <TableRow key={line.account_id}>
                    <TableCell className="font-mono">{line.marzban_username}</TableCell>
                    <TableCell className="text-right">{line.billable_gb} GB</TableCell>
                    <TableCell className="text-right text-muted-foreground">{formatToman(line.amount)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="flex items-center justify-between border-t border-border pt-3 text-sm font-semibold">
              <span>Total (not charged)</span>
              <span>{formatToman(invoiceQuery.data.total_amount)}</span>
            </div>
          </>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button variant="outline" onClick={() => resetMutation.mutate()} disabled={resetMutation.isPending}>
            {resetMutation.isPending ? 'Resetting…' : 'Reset without charging'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
