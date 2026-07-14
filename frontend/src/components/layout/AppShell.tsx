import { NavLink, Outlet } from 'react-router-dom'
import { LayoutDashboard, Users, Building2, Network, LogOut, RefreshCw, Wallet } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { toast } from 'sonner'
import { useAuth } from '@/lib/auth'
import { syncApi, apiErrorMessage } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { SettingsDialog } from '@/components/SettingsDialog'
import { cn } from '@/lib/utils'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, end: true },
  { to: '/customers', label: 'Customers', icon: Users },
  { to: '/groups', label: 'Groups', icon: Building2 },
  { to: '/accounts', label: 'Accounts', icon: Network },
  { to: '/finance', label: 'Finance', icon: Wallet },
]

export function AppShell() {
  const { logout } = useAuth()
  const syncMutation = useMutation({
    mutationFn: syncApi.run,
    onSuccess: (data: { marzban_user_count: number; created: number; updated: number }) => {
      toast.success(`Synced ${data.marzban_user_count} Marzban users (${data.created} new)`)
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-card">
        <div className="flex h-14 items-center border-b border-border px-5">
          <span className="font-semibold tracking-tight">VPN Reseller</span>
        </div>
        <nav className="flex flex-1 flex-col gap-1 p-3">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                  isActive ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
                )
              }
            >
              <item.icon className="h-4 w-4" />
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="flex flex-col gap-1 border-t border-border p-3">
          <Button variant="ghost" size="sm" className="justify-start gap-2" onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
            <RefreshCw className={cn('h-4 w-4', syncMutation.isPending && 'animate-spin')} />
            Sync with Marzban
          </Button>
          <SettingsDialog />
          <Button variant="ghost" size="sm" className="justify-start gap-2 text-muted-foreground" onClick={logout}>
            <LogOut className="h-4 w-4" />
            Log out
          </Button>
        </div>
      </aside>
      <main className="flex-1 overflow-auto bg-background">
        <div className="mx-auto max-w-7xl p-6">
          <Outlet />
        </div>
      </main>
    </div>
  )
}
