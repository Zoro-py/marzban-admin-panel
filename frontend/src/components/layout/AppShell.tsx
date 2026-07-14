import { NavLink, Outlet } from 'react-router-dom'
import { LayoutDashboard, Users, Building2, Network, LogOut, Moon, RefreshCw, Search, Sun, Wallet } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useAuth } from '@/lib/auth'
import { useTheme } from '@/lib/theme'
import { syncApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { SettingsDialog } from '@/components/SettingsDialog'
import { CommandPalette } from '@/components/CommandPalette'
import { AccountInspector } from '@/components/accounts/AccountInspector'
import { cn, formatAgo } from '@/lib/utils'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/customers', label: 'Customers', icon: Users },
  { to: '/groups', label: 'Groups', icon: Building2 },
  { to: '/accounts', label: 'Accounts', icon: Network },
  { to: '/finance', label: 'Finance', icon: Wallet },
]

export function AppShell() {
  const { logout } = useAuth()
  const { resolved, setTheme } = useTheme()
  const queryClient = useQueryClient()

  const statusQuery = useQuery({ queryKey: ['sync', 'status'], queryFn: syncApi.status, refetchInterval: 60_000 })
  const syncMutation = useMutation({
    mutationFn: syncApi.run,
    onSuccess: (data: { marzban_user_count: number; created: number }) => {
      toast.success(`Synced ${data.marzban_user_count} Marzban users (${data.created} new)`)
      queryClient.invalidateQueries()
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 z-30 flex w-52 flex-col border-r border-border bg-card">
        <div className="flex h-12 items-center gap-2 px-4">
          <span className="flex h-5 w-5 items-center justify-center rounded bg-primary font-mono text-[11px] font-bold text-primary-foreground">V</span>
          <span className="text-[13px] font-semibold tracking-tight">Reseller Console</span>
        </div>

        <button
          type="button"
          onClick={() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }))}
          className="mx-3 mb-2 flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:border-input hover:text-foreground"
        >
          <Search className="h-3.5 w-3.5" />
          <span className="flex-1 text-left">Search…</span>
          <kbd className="rounded border border-border bg-muted px-1 text-[10px]">⌘K</kbd>
        </button>

        <nav className="flex flex-1 flex-col gap-0.5 px-3">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] font-medium transition-colors',
                  isActive
                    ? 'bg-accent text-foreground'
                    : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
                )
              }
            >
              {({ isActive }) => (
                <>
                  <item.icon className={cn('h-4 w-4', isActive ? 'text-primary' : 'text-muted-foreground/70')} />
                  {item.label}
                </>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="flex flex-col gap-0.5 border-t border-border p-3">
          <button
            type="button"
            onClick={() => syncMutation.mutate()}
            disabled={syncMutation.isPending}
            className="flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-left text-xs text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground disabled:opacity-60"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', syncMutation.isPending && 'animate-spin')} />
            <span className="flex flex-col leading-tight">
              <span className="font-medium">{syncMutation.isPending ? 'Syncing…' : 'Sync with Marzban'}</span>
              <span className="text-[11px] text-muted-foreground/70">
                {statusQuery.data?.last_synced_at ? `synced ${formatAgo(statusQuery.data.last_synced_at)}` : 'never synced'}
              </span>
            </span>
          </button>
          <SettingsDialog />
          <div className="flex items-center justify-between px-1">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  onClick={() => setTheme(resolved === 'dark' ? 'light' : 'dark')}
                  aria-label="Toggle theme"
                >
                  {resolved === 'dark' ? <Sun /> : <Moon />}
                </Button>
              </TooltipTrigger>
              <TooltipContent>Switch to {resolved === 'dark' ? 'light' : 'dark'} theme</TooltipContent>
            </Tooltip>
            <Button size="sm" variant="ghost" className="gap-1.5 text-xs" onClick={logout}>
              <LogOut className="h-3.5 w-3.5" />
              Log out
            </Button>
          </div>
        </div>
      </aside>

      <main className="ml-52 flex-1 overflow-auto bg-background">
        <div className="mx-auto max-w-[1200px] px-5 py-5">
          <Outlet />
        </div>
      </main>

      <CommandPalette />
      <AccountInspector />
    </div>
  )
}
