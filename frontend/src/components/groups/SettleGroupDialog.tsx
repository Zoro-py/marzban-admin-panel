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
import { ReceiptText } from 'lucide-react'

export function SettleGroupDialog({ groupId }: { groupId: number }) {
  const [open, setOpen] = React.useState(false)
  const queryClient = useQueryClient()

  const invoiceQuery = useQuery({
    queryKey: ['groups', groupId, 'invoice'],
    queryFn: () => groupsApi.invoice(groupId),
    enabled: open,
  })

  const settleMutation = useMutation({
    mutationFn: () => groupsApi.settle(groupId),
    onSuccess: (result: { charged_amount: number }) => {
      toast.success(`Settled — charged ${formatToman(result.charged_amount)}`)
      queryClient.invalidateQueries({ queryKey: ['groups'] })
      queryClient.invalidateQueries({ queryKey: ['ledger'] })
      queryClient.invalidateQueries({ queryKey: ['reports'] })
      setOpen(false)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

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
