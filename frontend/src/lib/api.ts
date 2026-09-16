import axios, { AxiosError } from 'axios'
import type {
  Account,
  AccountEvent,
  AccountInvoice,
  AccountRow,
  Balance,
  BillingMode,
  BulkAccountPreview,
  BulkAccountRequest,
  BulkAccountResult,
  Customer,
  CustomerWithBalance,
  Delegate,
  DelegateUpsert,
  FinanceSummary,
  Group,
  GroupInvoice,
  GroupWithBalance,
  LedgerEntry,
  MonthlySettlementBatch,
  MonthlySettlementRunResult,
  NextPlan,
  OnlineHistory,
  OnlineHistoryRange,
  ReportSummary,
  ShopOrder,
  ShopSettings,
  ShopTopup,
  ShopTopupStatus,
  ShopUser,
  ShopWalletEntry,
  SyncStatus,
  SystemStatus,
  UpcomingRenewal,
} from './types'

const TOKEN_KEY = 'vpn_dashboard_token'

// "Remember me" checked -> localStorage (survives closing the browser, paired
// with a long-lived JWT from the backend). Unchecked -> sessionStorage (gone
// the moment the tab/browser closes, paired with the normal short-lived JWT).
// Both are checked on read so an existing session doesn't break if the choice
// changes; both are cleared on logout so a stale copy can't linger in the other.
export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY) ?? sessionStorage.getItem(TOKEN_KEY),
  set: (token: string, remember: boolean) => {
    if (remember) {
      localStorage.setItem(TOKEN_KEY, token)
      sessionStorage.removeItem(TOKEN_KEY)
    } else {
      sessionStorage.setItem(TOKEN_KEY, token)
      localStorage.removeItem(TOKEN_KEY)
    }
  },
  clear: () => {
    localStorage.removeItem(TOKEN_KEY)
    sessionStorage.removeItem(TOKEN_KEY)
  },
}

export const api = axios.create({
  // `||`, not `??` — an unset PUBLIC_BACKEND_URL build arg comes through as an
  // EMPTY STRING (Docker/Compose default), which `??` treats as a valid value
  // and would silently point every request at same-origin (the frontend's own
  // nginx) instead of falling back to this default. Found while diagnosing a
  // report of "nothing on the page works" that turned out to be exactly this.
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000',
})

api.interceptors.request.use((config) => {
  const token = tokenStore.get()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    if (error.response?.status === 401) {
      tokenStore.clear()
      if (window.location.pathname !== '/login') {
        window.location.assign('/login')
      }
    }
    return Promise.reject(error)
  },
)

export function apiErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = (error.response?.data as { detail?: string } | undefined)?.detail
    return detail ?? error.message
  }
  return error instanceof Error ? error.message : 'Unexpected error'
}

// ---- auth ----
export async function login(username: string, password: string, rememberMe: boolean): Promise<string> {
  const { data } = await api.post<{ access_token: string }>('/api/auth/login', {
    username,
    password,
    remember_me: rememberMe,
  })
  return data.access_token
}

// ---- customers ----
export const customersApi = {
  list: async () => (await api.get<CustomerWithBalance[]>('/api/customers')).data,
  get: async (id: number) => (await api.get<CustomerWithBalance>(`/api/customers/${id}`)).data,
  create: async (body: { name: string; contact?: string; is_group_rep?: boolean }) =>
    (await api.post<Customer>('/api/customers', body)).data,
  update: async (id: number, body: Partial<{ name: string; contact: string; is_group_rep: boolean }>) =>
    (await api.patch<Customer>(`/api/customers/${id}`, body)).data,
  accounts: async (id: number) => (await api.get<AccountRow[]>(`/api/customers/${id}/accounts`)).data,
}

// ---- groups ----
export const groupsApi = {
  list: async () => (await api.get<GroupWithBalance[]>('/api/groups')).data,
  get: async (id: number) => (await api.get<GroupWithBalance>(`/api/groups/${id}`)).data,
  create: async (body: {
    name: string
    representative_customer_id: number
    billing_cycle_days?: number
    rate_per_gb?: number
    billing_mode?: BillingMode
  }) => (await api.post<Group>('/api/groups', body)).data,
  update: async (
    id: number,
    body: Partial<{ name: string; billing_cycle_days: number; rate_per_gb: number; billing_mode: BillingMode }>,
  ) => (await api.patch<Group>(`/api/groups/${id}`, body)).data,
  accounts: async (id: number) => (await api.get<AccountRow[]>(`/api/groups/${id}/accounts`)).data,
  invoice: async (id: number) => (await api.get<GroupInvoice>(`/api/groups/${id}/invoice`)).data,
  settle: async (id: number, body?: { mark_paid?: boolean; pay_scope?: 'full' | 'prior_only' }) =>
    (await api.post(`/api/groups/${id}/settle`, body ?? {})).data,
  // Settle exactly one member without closing the whole group's cycle — for
  // "this one person paid, the rest of the group isn't ready yet."
  settleMember: async (groupId: number, accountId: number, body?: { mark_paid?: boolean; pay_scope?: 'full' | 'prior_only' }) =>
    (await api.post(`/api/groups/${groupId}/members/${accountId}/settle`, body ?? {})).data,
  resetCycle: async (id: number) => (await api.post(`/api/groups/${id}/reset-cycle`)).data,
}

// ---- accounts ----
export const accountsApi = {
  list: async (params?: { unassigned_only?: boolean; customer_id?: number; group_id?: number }) =>
    (await api.get<AccountRow[]>('/api/accounts', { params })).data,
  get: async (id: number) => (await api.get<AccountRow>(`/api/accounts/${id}`)).data,
  create: async (body: {
    marzban_username: string
    customer_id?: number | null
    group_id?: number | null
    role?: 'primary' | 'sub'
    rate_per_gb?: number | null
    expire?: number | null
    data_limit?: number | null
    status?: string
    note?: string
  }) => (await api.post<Account>('/api/accounts', body)).data,
  // ---- bulk ("family") creation ----
  // Both take the SAME request shape on purpose: the preview is only
  // trustworthy as long as it is computed from exactly what create would send.
  previewBulk: async (body: BulkAccountRequest) =>
    (await api.post<BulkAccountPreview>('/api/accounts/bulk/preview', body)).data,
  // Slow by nature — one Marzban create per account — so this deliberately
  // overrides the default timeout rather than letting a 40-account batch look
  // like a failure to the operator while it is in fact still running.
  createBulk: async (body: BulkAccountRequest) =>
    (await api.post<BulkAccountResult>('/api/accounts/bulk', body, { timeout: 180_000 })).data,
  updateRelationship: async (id: number, body: { customer_id?: number | null; group_id?: number | null; role?: 'primary' | 'sub' }) =>
    (await api.patch<Account>(`/api/accounts/${id}/relationship`, body)).data,
  updateBilling: async (
    id: number,
    body: { rate_per_gb?: number | null; billing_mode?: BillingMode; clear_rate?: boolean; auto_renew_enabled?: boolean },
  ) => (await api.patch<Account>(`/api/accounts/${id}/billing`, body)).data,
  adjust: async (
    id: number,
    body: { extend_days?: number; extend_gb?: number; set_expire?: number; set_data_limit_gb?: number; note?: string },
  ) => (await api.post<Account>(`/api/accounts/${id}/adjust`, body)).data,
  reset: async (id: number, body: { charge_amount?: number; note?: string }) =>
    (await api.post<Account>(`/api/accounts/${id}/reset`, body)).data,
  invoice: async (id: number) => (await api.get<AccountInvoice>(`/api/accounts/${id}/invoice`)).data,
  settle: async (id: number, body?: { mark_paid?: boolean; pay_scope?: 'full' | 'prior_only' }) =>
    (await api.post(`/api/accounts/${id}/settle`, body ?? {})).data,
  events: async (id: number) => (await api.get<AccountEvent[]>(`/api/accounts/${id}/events`)).data,
  // ---- next plan ----
  getNextPlan: async (id: number) => {
    try {
      return (await api.get<NextPlan>(`/api/accounts/${id}/next-plan`)).data
    } catch (e) {
      if (axios.isAxiosError(e) && e.response?.status === 404) return null
      throw e
    }
  },
  setNextPlan: async (id: number, body: { data_limit_gb: number; duration_days: number; billing_mode?: BillingMode | null }) =>
    (await api.post<NextPlan>(`/api/accounts/${id}/next-plan`, body)).data,
  cancelNextPlan: async (id: number) => (await api.delete(`/api/accounts/${id}/next-plan`)).data,
  deleteAccount: async (id: number) => (await api.post(`/api/accounts/${id}/delete`)).data,
}

// ---- ledger ----
export const ledgerApi = {
  list: async (params?: { customer_id?: number; group_id?: number; account_id?: number }) =>
    (await api.get<LedgerEntry[]>('/api/ledger', { params })).data,
  create: async (body: {
    type: 'charge' | 'credit'
    amount: number
    customer_id?: number | null
    group_id?: number | null
    account_id?: number | null
    note?: string
  }) => (await api.post<LedgerEntry>('/api/ledger', body)).data,
  balance: async (params: { customer_id?: number; group_id?: number; account_id?: number; since?: string }) =>
    (await api.get<Balance>('/api/ledger/balance', { params })).data,
}

// ---- reports / sync ----
export const reportsApi = {
  summary: async () => (await api.get<ReportSummary>('/api/reports/summary')).data,
  finance: async () => (await api.get<FinanceSummary>('/api/reports/finance')).data,
  onlineHistory: async (range: OnlineHistoryRange) =>
    (await api.get<OnlineHistory>('/api/reports/online-history', { params: { range } })).data,
  systemStatus: async () => (await api.get<SystemStatus>('/api/reports/system-status')).data,
  upcomingRenewals: async () => (await api.get<UpcomingRenewal[]>('/api/reports/upcoming-renewals')).data,
}

export const syncApi = {
  run: async () => (await api.post('/api/sync/run')).data,
  status: async () => (await api.get<SyncStatus>('/api/sync/status')).data,
}

// ---- payg monthly settlements ----
export const paygMonthlyApi = {
  run: async () => (await api.post<MonthlySettlementRunResult>('/api/payg-monthly/run')).data,
  periods: async () => (await api.get<string[]>('/api/payg-monthly/periods')).data,
  batches: async (period?: string) =>
    (await api.get<{ period: string | null; rows: MonthlySettlementBatch[] }>('/api/payg-monthly/batches', {
      params: period ? { period } : undefined,
    })).data,
  markPaid: async (batchId: number) =>
    (await api.post<MonthlySettlementBatch>(`/api/payg-monthly/batches/${batchId}/mark-paid`)).data,
}

// ---- settings ----
export const settingsApi = {
  get: async () => (await api.get<{ default_rate_per_gb: number | null }>('/api/settings')).data,
  update: async (body: { default_rate_per_gb: number | null }) =>
    (await api.patch<{ default_rate_per_gb: number | null }>('/api/settings', body)).data,
}

// ---- delegates (operator-only grant management) ----
export const delegatesApi = {
  list: async () => (await api.get<Delegate[]>('/api/delegate')).data,
  // POST /api/delegate is a PARTIAL upsert keyed by telegram_id: the backend
  // only writes fields actually present in the body (pydantic exclude_unset).
  // A create sends the full shape; an edit must send ONLY the changed fields
  // (plus telegram_id) — re-sending untouched fields with their defaults
  // would silently reset the delegate's stored values (exactly the bug the
  // bot's /delegate_cap was split out of /delegate_add to avoid). When
  // changing scope, send BOTH customer_id and group_id explicitly (one of
  // them null) — the backend requires exactly one non-null across the
  // provided fields, and an omitted field keeps its old value.
  upsert: async (body: DelegateUpsert) => (await api.post<Delegate>('/api/delegate', body)).data,
  deactivate: async (id: number) => (await api.post<Delegate>(`/api/delegate/${id}/deactivate`)).data,
}

// ---- database backup (same pipeline as the nightly schedule) ----
export const backupApi = {
  run: async () => (await api.post<{ sent_at: string; filename: string; size_bytes: number }>('/api/backup/run')).data,
}

// ---- self-serve shop ----
export const shopApi = {
  settings: async () => (await api.get<ShopSettings>('/api/shop/settings')).data,
  updateSettings: async (body: Partial<Omit<ShopSettings, 'id'>>) =>
    (await api.patch<ShopSettings>('/api/shop/settings', body)).data,
  users: async () => (await api.get<ShopUser[]>('/api/shop/users')).data,
  updateUser: async (id: number, body: { is_blocked?: boolean; customer_id?: number | null }) =>
    (await api.patch<ShopUser>(`/api/shop/users/${id}`, body)).data,
  walletEntries: async (id: number) =>
    (await api.get<ShopWalletEntry[]>(`/api/shop/users/${id}/wallet`)).data,
  adjustWallet: async (id: number, body: { amount: number; note?: string }) =>
    (await api.post<ShopUser>(`/api/shop/users/${id}/wallet`, body)).data,
  topups: async (status?: ShopTopupStatus) =>
    (await api.get<ShopTopup[]>('/api/shop/topups', { params: status ? { status } : undefined })).data,
  approveTopup: async (id: number, amount?: number) =>
    (await api.post<ShopTopup>(`/api/shop/topups/${id}/approve`, amount ? { amount } : {})).data,
  rejectTopup: async (id: number, reason?: string) =>
    (await api.post<ShopTopup>(`/api/shop/topups/${id}/reject`, reason ? { reason } : {})).data,
  orders: async () => (await api.get<ShopOrder[]>('/api/shop/orders')).data,
}
