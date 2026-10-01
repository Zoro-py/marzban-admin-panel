import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Copy } from 'lucide-react'
import { customersApi, groupsApi, delegatesApi, apiErrorMessage } from '@/lib/api'
import type { DelegateInvite } from '@/lib/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { SearchableSelect } from '@/components/ui/searchable-select'
import { formatDate } from '@/lib/utils'

type ScopeKind = 'customer' | 'group'

// Same copy flow as BulkAccountDialog's onCopy — no shared clipboard helper
// exists in the app, each surface wires navigator.clipboard inline.
function copyInviteLink(text: string) {
  navigator.clipboard?.writeText(text).then(
    () => toast.success('Invite link copied — send it to the customer'),
    () => toast.error('Could not copy — select the link and copy it manually'),
  )
}

// Mint a one-time delegate invite link (POST /api/delegate/invite) without
// asking for the customer's numeric Telegram id — data sourcing mirrors
// DelegateDialog's scope picker (same queries, same searchable select).
export function InviteDelegateDialog({ trigger }: { trigger: React.ReactNode }) {
  const [open, setOpen] = React.useState(false)
  const [scopeKind, setScopeKind] = React.useState<ScopeKind>('customer')
  const [scopeId, setScopeId] = React.useState('')
  const [invite, setInvite] = React.useState<DelegateInvite | null>(null)
  const queryClient = useQueryClient()

  React.useEffect(() => {
    if (!open) return
    setScopeKind('customer')
    setScopeId('')
    setInvite(null)
  }, [open])

  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: () => customersApi.list(), enabled: open && scopeKind === 'customer' })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: () => groupsApi.list(), enabled: open && scopeKind === 'group' })

  const mutation = useMutation({
    mutationFn: () =>
      delegatesApi.invite(scopeKind === 'customer' ? { customer_id: Number(scopeId) } : { group_id: Number(scopeId) }),
    onSuccess: (inv) => {
      // Keep the dialog open showing the minted link — copying it is the
      // whole point; the table refreshes underneath via ['delegates'].
      setInvite(inv)
      toast.success(`Invite link created for ${inv.scope_name}.`)
      queryClient.invalidateQueries({ queryKey: ['delegates'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{invite ? 'Invite link created' : 'Invite a delegate'}</DialogTitle>
          <DialogDescription>
            {invite
              ? 'One-time link: the first Telegram account that opens it claims the delegate access. Send it to the customer and discard it here if it goes stale.'
              : 'No Telegram ID needed — pick a customer or group and send them the link. They claim the access themselves by opening it in delegate_bot.'}
          </DialogDescription>
        </DialogHeader>

        {invite ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="delegate-invite-url">Invite link for {invite.scope_name}</Label>
            <div className="flex items-center gap-2">
              <Input
                id="delegate-invite-url"
                readOnly
                value={invite.invite_url}
                onFocus={(e) => e.currentTarget.select()}
                className="font-mono text-xs"
              />
              <Button type="button" size="sm" className="gap-1.5 shrink-0" onClick={() => copyInviteLink(invite.invite_url)}>
                <Copy className="h-3.5 w-3.5" /> Copy
              </Button>
            </div>
            <p className="text-[11px] text-muted-foreground">
              Expires {formatDate(invite.claim_expires_at)} — after that the link stops working and a fresh one can be
              minted.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            <Label>Scope — whose accounts they can self-manage</Label>
            <div className="grid grid-cols-[auto_1fr] items-center gap-2">
              <Select
                value={scopeKind}
                onValueChange={(v) => {
                  setScopeKind(v as ScopeKind)
                  setScopeId('')
                }}
              >
                <SelectTrigger className="w-28">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="customer">Customer</SelectItem>
                  <SelectItem value="group">Group</SelectItem>
                </SelectContent>
              </Select>
              {scopeKind === 'customer' ? (
                <SearchableSelect
                  value={scopeId}
                  onValueChange={setScopeId}
                  placeholder="Select a customer"
                  searchPlaceholder="Search customers…"
                  options={(customersQuery.data ?? []).map((c) => ({ value: String(c.id), label: c.name }))}
                />
              ) : (
                <SearchableSelect
                  value={scopeId}
                  onValueChange={setScopeId}
                  placeholder="Select a group"
                  searchPlaceholder="Search groups…"
                  options={(groupsQuery.data ?? []).map((g) => ({ value: String(g.id), label: g.name }))}
                />
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              Exactly one pending invite can exist per scope — minting a second one for the same{' '}
              {scopeKind === 'customer' ? 'customer' : 'group'} is refused until the first is discarded or claimed.
            </p>
          </div>
        )}

        <DialogFooter>
          {invite ? (
            <Button onClick={() => setOpen(false)}>Done</Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button onClick={() => mutation.mutate()} disabled={scopeId === '' || mutation.isPending}>
                {mutation.isPending ? 'Creating…' : 'Create invite link'}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
