import { Suspense } from 'react'
import { Route, Routes } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { RequireAuth } from '@/components/layout/RequireAuth'
import { GlobalErrorBoundary } from '@/components/GlobalErrorBoundary'
import { recoverableLazy } from '@/lib/chunkRecovery'

const LoginPage = recoverableLazy(() => import('@/pages/LoginPage').then(m => ({ default: m.LoginPage })))
const DashboardPage = recoverableLazy(() => import('@/pages/DashboardPage').then(m => ({ default: m.DashboardPage })))
const CustomersPage = recoverableLazy(() => import('@/pages/CustomersPage').then(m => ({ default: m.CustomersPage })))
const CustomerDetailPage = recoverableLazy(() => import('@/pages/CustomerDetailPage').then(m => ({ default: m.CustomerDetailPage })))
const GroupsPage = recoverableLazy(() => import('@/pages/GroupsPage').then(m => ({ default: m.GroupsPage })))
const GroupDetailPage = recoverableLazy(() => import('@/pages/GroupDetailPage').then(m => ({ default: m.GroupDetailPage })))
const AccountsPage = recoverableLazy(() => import('@/pages/AccountsPage').then(m => ({ default: m.AccountsPage })))
const FinancePage = recoverableLazy(() => import('@/pages/FinancePage').then(m => ({ default: m.FinancePage })))
const ChargeHistoryPage = recoverableLazy(() => import('@/pages/ChargeHistoryPage').then(m => ({ default: m.ChargeHistoryPage })))
const MonthlySettlementsPage = recoverableLazy(() => import('@/pages/MonthlySettlementsPage').then(m => ({ default: m.MonthlySettlementsPage })))
const ShopPage = recoverableLazy(() => import('@/pages/ShopPage').then(m => ({ default: m.ShopPage })))
const DelegatesPage = recoverableLazy(() => import('@/pages/DelegatesPage').then(m => ({ default: m.DelegatesPage })))
const ServersPage = recoverableLazy(() => import('@/pages/ServersPage').then(m => ({ default: m.ServersPage })))
const ServerDetailPage = recoverableLazy(() => import('@/pages/ServerDetailPage').then(m => ({ default: m.ServerDetailPage })))

function GlobalLoader() {
  return (
    <div className="flex h-screen w-full items-center justify-center">
      <div className="text-sm text-muted-foreground">Loading...</div>
    </div>
  )
}

function NotFoundPage() {
  return (
    <div className="flex h-screen w-full flex-col items-center justify-center gap-4">
      <h1 className="text-4xl font-bold">404</h1>
      <p className="text-muted-foreground">Page not found</p>
      <a href="/" className="text-sm text-primary hover:underline">Return to Dashboard</a>
    </div>
  )
}

function App() {
  return (
    // Outer boundary (over Suspense too): a chunk failure on /login — which is
    // outside the RequireAuth boundary below — used to unmount the whole tree
    // into a blank page. The inner boundary keeps a page crash inside the
    // shell; this one catches everything else.
    <GlobalErrorBoundary>
      <Suspense fallback={<GlobalLoader />}>
        <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route
            element={
              <GlobalErrorBoundary>
                <AppShell />
              </GlobalErrorBoundary>
            }
          >
            <Route path="/" element={<DashboardPage />} />
            <Route path="/customers" element={<CustomersPage />} />
            <Route path="/customers/:id" element={<CustomerDetailPage />} />
            <Route path="/groups" element={<GroupsPage />} />
            <Route path="/groups/:id" element={<GroupDetailPage />} />
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/finance" element={<FinancePage />} />
            <Route path="/history" element={<ChargeHistoryPage />} />
            <Route path="/shop" element={<ShopPage />} />
            <Route path="/delegates" element={<DelegatesPage />} />
            <Route path="/servers" element={<ServersPage />} />
            <Route path="/servers/:serverId" element={<ServerDetailPage />} />
            <Route path="/monthly-settlements" element={<MonthlySettlementsPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
        <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </Suspense>
    </GlobalErrorBoundary>
  )
}

export default App

