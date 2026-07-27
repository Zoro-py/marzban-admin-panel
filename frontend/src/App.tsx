import { Suspense, lazy } from 'react'
import { Route, Routes } from 'react-router-dom'
import { AppShell } from '@/components/layout/AppShell'
import { RequireAuth } from '@/components/layout/RequireAuth'
import { GlobalErrorBoundary } from '@/components/GlobalErrorBoundary'

const LoginPage = lazy(() => import('@/pages/LoginPage').then(m => ({ default: m.LoginPage })))
const DashboardPage = lazy(() => import('@/pages/DashboardPage').then(m => ({ default: m.DashboardPage })))
const CustomersPage = lazy(() => import('@/pages/CustomersPage').then(m => ({ default: m.CustomersPage })))
const CustomerDetailPage = lazy(() => import('@/pages/CustomerDetailPage').then(m => ({ default: m.CustomerDetailPage })))
const GroupsPage = lazy(() => import('@/pages/GroupsPage').then(m => ({ default: m.GroupsPage })))
const GroupDetailPage = lazy(() => import('@/pages/GroupDetailPage').then(m => ({ default: m.GroupDetailPage })))
const AccountsPage = lazy(() => import('@/pages/AccountsPage').then(m => ({ default: m.AccountsPage })))
const FinancePage = lazy(() => import('@/pages/FinancePage').then(m => ({ default: m.FinancePage })))

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
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </Suspense>
  )
}

export default App

