import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { groupsApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
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
import { Money } from '@/components/Money'
import { cn, formatToman } from '@/lib/utils'
import { ReceiptText } from 'lucide-react'

/** "Settle cycle" POSTS A CHARGE — the estimate becomes a real, formal
 * debt — it is NOT, by itself, a record that payment was received. The
 * "payment received now too" checkbox closes that gap for the common case
 * (the representative customer pays on the spot): checked, it also posts a
 * matching credit, so the balance nets back to settled instead of staying
 * owed. See SettleAccountButton for the same pattern at the account level. */
export function SettleGroupDialog({ groupId, currentBalance }: { groupId: number; currentBalance: number }) {
  const [open, setOpen] = React.useState(false)
  const [markPaid, setMarkPaid] = React.useState(true)
  const queryClient = useQueryClient()

  const invoiceQuery = useQuery({
    queryKey: ['groups', groupId, 'invoice'],
    queryFn: () => groupsApi.invoice(groupId),
    enabled: open,
  })

  const settleMutation = useMutation({
    mutationFn: () => groupsApi.settle(groupId, { mark_paid: markPaid }),
    onSuccess: () => {
      toast.success(markPaid ? 'Settled — paid in full' : 'Settled — charged, still owed')
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const amount = invoiceQuery.data?.total_amount ?? 0
  const resultingBalance = currentBalance + amount - (markPaid ? amount : 0)

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" className="gap-1.5">
          <ReceiptText className="h-4 w-4" /> Settle cycle
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Settle this billing cycle</DialogTitle>
          <DialogDescription>
            Posts one debt entry for the usage below against the representative customer, then resets the cycle.
            This bills it — it does not by itself record a payment.
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
                  <TableHead className="text-right">Amount</TableHead>
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
                    <TableCell className="text-right">{formatToman(line.amount)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            <div className="flex items-center justify-between border-t border-border pt-3 text-sm font-semibold">
              <span>Total</span>
              <span>{formatToman(invoiceQuery.data.total_amount)}</span>
            </div>
          </>
        )}

        <label className="flex cursor-pointer items-start gap-2 rounded-md border border-border bg-muted/40 p-2.5 text-xs">
          <Checkbox checked={markPaid} onCheckedChange={(v) => setMarkPaid(v === true)} className="mt-0.5" />
          <span className="flex-1">
            <span className="font-medium">Payment received now too</span>
            <p className="mt-0.5 text-muted-foreground">
              Check this if the representative customer is paying right now — also posts a matching credit so the
              balance below ends up settled, not owed.
            </p>
          </span>
        </label>

        <div
          className={cn(
            'flex items-center justify-between rounded-md border p-2.5 text-xs',
            resultingBalance > 0 && 'border-destructive/30 bg-destructive/5',
            resultingBalance < 0 && 'border-credit/30 bg-credit/5',
            resultingBalance === 0 && 'border-success/30 bg-success/5',
          )}
        >
          <span className="text-muted-foreground">Balance after this:</span>
          <Money amount={resultingBalance} zero="settled" className="font-semibold" />
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => settleMutation.mutate()}
            disabled={!invoiceQuery.data || invoiceQuery.data.total_amount <= 0 || settleMutation.isPending}
          >
            {settleMutation.isPending ? 'Settling…' : 'Confirm & charge'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
