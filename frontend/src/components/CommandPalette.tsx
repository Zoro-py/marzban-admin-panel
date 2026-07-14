import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  Building2,
  LayoutDashboard,
  Moon,
  Network,
  RefreshCw,
  Search,
  Sun,
  User,
  Users,
  Wallet,
} from 'lucide-react'
import { accountsApi, customersApi, groupsApi, syncApi, apiErrorMessage } from '@/lib/api'
import { useTheme } from '@/lib/theme'
import { useOpenAccountInspector } from '@/components/accounts/AccountInspector'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

interface Item {
  id: string
  label: string
  hint?: string
  icon: React.ComponentType<{ className?: string }>
  keywords: string
  run: () => void
}

/** Ctrl/Cmd+K jump-anywhere: pages, any account / customer / group by name,
 * and the global actions (sync, theme). For a tool used dozens of times a day,
 * "type three letters, hit enter" beats any navigation tree. */
export function CommandPalette() {
  const [open, setOpen] = React.useState(false)
  const [query, setQuery] = React.useState('')
  const [active, setActive] = React.useState(0)
  const navigate = useNavigate()
  const openAccount = useOpenAccountInspector()
  const { resolved, setTheme } = useTheme()
  const queryClient = useQueryClient()
  const listRef = React.useRef<HTMLDivElement | null>(null)

  React.useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  React.useEffect(() => {
    if (open) {
      setQuery('')
      setActive(0)
    }
  }, [open])

  const accountsQuery = useQuery({ queryKey: ['accounts', 'palette'], queryFn: () => accountsApi.list(), enabled: open })
  const customersQuery = useQuery({ queryKey: ['customers'], queryFn: customersApi.list, enabled: open })
  const groupsQuery = useQuery({ queryKey: ['groups'], queryFn: groupsApi.list, enabled: open })

  const syncMutation = useMutation({
    mutationFn: syncApi.run,
    onSuccess: (data: { marzban_user_count: number; created: number }) => {
      toast.success(`Synced ${data.marzban_user_count} Marzban users (${data.created} new)`)
      queryClient.invalidateQueries()
    },
    onError: (err) => toast.error(apiErrorMessage(err)),
  })

  const items = React.useMemo<Item[]>(() => {
    const close = () => setOpen(false)
    const pages: Item[] = [
      { id: 'p-dash', label: 'Dashboard', icon: LayoutDashboard, keywords: 'dashboard home overview', run: () => { navigate('/'); close() } },
      { id: 'p-cust', label: 'Customers', icon: Users, keywords: 'customers people billing contacts', run: () => { navigate('/customers'); close() } },
      { id: 'p-grp', label: 'Groups', icon: Building2, keywords: 'groups companies', run: () => { navigate('/groups'); close() } },
      { id: 'p-acct', label: 'Accounts', icon: Network, keywords: 'accounts users marzban', run: () => { navigate('/accounts'); close() } },
      { id: 'p-fin', label: 'Finance', icon: Wallet, keywords: 'finance money revenue ledger rates', run: () => { navigate('/finance'); close() } },
    ]
    const actions: Item[] = [
      {
        id: 'a-sync',
        label: 'Sync with Marzban now',
        icon: RefreshCw,
        keywords: 'sync refresh marzban pull',
        run: () => { syncMutation.mutate(); close() },
      },
      {
        id: 'a-theme',
        label: resolved === 'dark' ? 'Switch to light theme' : 'Switch to dark theme',
        icon: resolved === 'dark' ? Sun : Moon,
        keywords: 'theme dark light mode toggle',
        run: () => { setTheme(resolved === 'dark' ? 'light' : 'dark'); close() },
      },
    ]
    const accounts: Item[] = (accountsQuery.data ?? []).map((a) => ({
      id: `acct-${a.id}`,
      label: a.marzban_username,
      hint: a.customer_name ?? a.group_name ?? 'unassigned',
      icon: User,
      keywords: `${a.marzban_username} ${a.customer_name ?? ''} ${a.group_name ?? ''}`.toLowerCase(),
      run: () => { openAccount(a.id); close() },
    }))
    const customers: Item[] = (customersQuery.data ?? []).map((c) => ({
      id: `cust-${c.id}`,
      label: c.name,
      hint: 'customer',
      icon: Users,
      keywords: `${c.name} ${c.contact ?? ''}`.toLowerCase(),
      run: () => { navigate(`/customers/${c.id}`); close() },
    }))
    const groups: Item[] = (groupsQuery.data ?? []).map((g) => ({
      id: `grp-${g.id}`,
      label: g.name,
      hint: 'group',
      icon: Building2,
      keywords: g.name.toLowerCase(),
      run: () => { navigate(`/groups/${g.id}`); close() },
    }))
    return [...pages, ...actions, ...accounts, ...customers, ...groups]
  }, [accountsQuery.data, customersQuery.data, groupsQuery.data, navigate, openAccount, resolved, setTheme, syncMutation])

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return items.slice(0, 12)
    return items.filter((i) => i.label.toLowerCase().includes(q) || i.keywords.includes(q)).slice(0, 12)
  }, [items, query])

  React.useEffect(() => setActive(0), [query])

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActive((a) => Math.min(a + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActive((a) => Math.max(a - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      filtered[active]?.run()
    }
  }

  React.useEffect(() => {
    listRef.current?.children[active]?.scrollIntoView({ block: 'nearest' })
  }, [active])

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="top-[20%] max-w-lg translate-y-0 gap-0 overflow-hidden p-0 [&>button]:hidden">
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <div className="flex items-center gap-2 border-b border-border px-3">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" />
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Jump to an account, customer, group, or page…"
            className="h-11 w-full bg-transparent text-[13px] outline-none placeholder:text-muted-foreground/70"
          />
          <kbd className="rounded border border-border bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">esc</kbd>
        </div>
        <div ref={listRef} className="max-h-80 overflow-y-auto p-1.5">
          {filtered.length === 0 && (
            <p className="px-3 py-6 text-center text-xs text-muted-foreground">Nothing matches “{query}”.</p>
          )}
          {filtered.map((item, i) => (
            <button
              key={item.id}
              onClick={item.run}
              onMouseMove={() => setActive(i)}
              className={cn(
                'flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-[13px]',
                i === active ? 'bg-accent text-accent-foreground' : 'text-foreground',
              )}
            >
              <item.icon className="h-4 w-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              {item.hint && <span className="shrink-0 text-[11px] text-muted-foreground">{item.hint}</span>}
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}
