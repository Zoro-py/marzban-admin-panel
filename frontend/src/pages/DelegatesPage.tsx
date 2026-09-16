import * as React from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { ShieldCheck, UserCog } from 'lucide-react'
import { delegatesApi, apiErrorMessage } from '@/lib/api'
import type { Delegate } from '@/lib/types'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { DelegateDialog } from '@/components/delegates/DelegateDialog'
import { formatToman } from '@/lib/utils'

function ScopeCell({ delegate }: { delegate: Delegate }) {
  return (
    <span className="flex items-center gap-1.5">
      <Badge variant={delegate.customer_id != null ? 'secondary' : 'outline'}>
        {delegate.customer_id != null ? 'customer' : 'group'}
      </Badge>
      <span className="font-medium">{delegate.scope_name}</span>
    </span>
  )
}

export function DelegatesPage() {
  React.useEffect(() => {
    document.title = 'Shiraze | Delegates'
  }, [])

  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({ queryKey: ['delegates'], queryFn: delegatesApi.list })

  const deactivateMutation = useMutation({
    mutationFn: (d: Delegate) => delegatesApi.deactivate(d.id),
    onSuccess: (d) => {
      toast.success(`Delegate access revoked for ${d.scope_name} — their bot session stops working immediately.`)
      queryClient.invalidateQueries({ queryKey: ['delegates'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const reactivateMutation = useMutation({
    mutationFn: (d: Delegate) =>
      // Re-granting is the same partial upsert the bot's /delegate_add uses:
      // posting just the telegram_id re-activates the row and touches nothing
      // else — the operator's configured limits all survive.
      delegatesApi.upsert({ telegram_id: d.telegram_id }),
    onSuccess: (d) => {
      toast.success(`Delegate access re-granted to ${d.scope_name} (Telegram id ${d.telegram_id}).`)
      queryClient.invalidateQueries({ queryKey: ['delegates'] })
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Delegates</h1>
          <p className="text-xs text-muted-foreground">
            Trusted customers or groups who run their own accounts via the delegate bot — create, renew and delete, but
            never any visibility into money or other customers.
          </p>
        </div>
        <DelegateDialog
          trigger={
            <Button size="sm" className="gap-1.5">
              <UserCog className="h-4 w-4" /> New delegate
            </Button>
          }
        />
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Scope</TableHead>
              <TableHead className="hidden sm:table-cell">Telegram ID</TableHead>
              <TableHead className="hidden md:table-cell">Credit limit</TableHead>
              <TableHead className="hidden text-right lg:table-cell">Daily cap</TableHead>
              <TableHead className="hidden lg:table-cell">Account defaults</TableHead>
              <TableHead className="text-right">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading &&
              Array.from({ length: 3 }).map((_, i) => (
                <TableRow key={i}>
                  <TableCell>
                    <Skeleton className="h-4 w-36" />
                  </TableCell>
                  <TableCell className="hidden sm:table-cell">
                    <Skeleton className="h-4 w-24" />
                  </TableCell>
                  <TableCell className="hidden md:table-cell">
                    <Skeleton className="h-4 w-16" />
                  </TableCell>
                  <TableCell className="hidden text-right lg:table-cell">
                    <Skeleton className="h-4 w-8 ml-auto" />
                  </TableCell>
                  <TableCell className="hidden lg:table-cell">
                    <Skeleton className="h-4 w-24" />
                  </TableCell>
                  <TableCell className="text-right">
                    <Skeleton className="h-4 w-20 ml-auto" />
                  </TableCell>
                </TableRow>
              ))}
            {!isLoading && data?.length === 0 && (
              <TableRow>
                <TableCell colSpan={6} className="py-8 text-center text-muted-foreground">
                  No delegates yet — grants created here (or via the bot's /delegate_add) let a trusted customer
                  self-manage their own accounts.
                </TableCell>
              </TableRow>
            )}
            {data?.map((d) => (
              <TableRow key={d.id}>
                <TableCell>
                  <span className="flex flex-col leading-tight">
                    <ScopeCell delegate={d} />
                    {d.label && <span className="text-[11px] text-muted-foreground">{d.label}</span>}
                  </span>
                </TableCell>
                <TableCell className="hidden font-mono text-xs tabular-nums sm:table-cell">{d.telegram_id}</TableCell>
                <TableCell className="hidden md:table-cell">
                  {d.credit_limit != null ? (
                    <span className="tabular-nums">{formatToman(d.credit_limit)}</span>
                  ) : (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <span className="text-xs text-muted-foreground">no limit</span>
                      </TooltipTrigger>
                      <TooltipContent>Unlimited trust — they can accumulate debt without a ceiling.</TooltipContent>
                    </Tooltip>
                  )}
                </TableCell>
                <TableCell className="hidden text-right tabular-nums lg:table-cell">{d.daily_create_cap}/day</TableCell>
                <TableCell className="hidden text-xs text-muted-foreground lg:table-cell">
                  <span className="font-mono">{d.username_prefix}N</span> · {d.default_duration_days}d
                </TableCell>
                <TableCell className="text-right">
                  <span className="flex items-center justify-end gap-1.5">
                    {d.is_active ? (
                      <Badge>
                        <ShieldCheck className="h-3 w-3" /> active
                      </Badge>
                    ) : (
                      <Badge variant="warning">revoked</Badge>
                    )}
                    <DelegateDialog
                      delegate={d}
                      trigger={
                        <Button size="sm" variant="ghost" className="h-7 px-2 text-xs">
                          Edit
                        </Button>
                      }
                    />
                    {d.is_active ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                        disabled={deactivateMutation.isPending}
                        onClick={() => {
                          if (
                            window.confirm(
                              `Revoke delegate access for ${d.scope_name} (Telegram id ${d.telegram_id})?\n\n` +
                                'Their bot session stops working immediately — accounts they already created are NOT touched.',
                            )
                        ) {
                          deactivateMutation.mutate(d)
                        }
                      }}
                    >
                        Revoke
                      </Button>
                    ) : (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-7 px-2 text-xs"
                        disabled={reactivateMutation.isPending}
                        onClick={() => reactivateMutation.mutate(d)}
                      >
                        Re-grant
                      </Button>
                    )}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
