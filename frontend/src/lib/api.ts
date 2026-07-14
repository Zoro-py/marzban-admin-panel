import axios, { AxiosError } from 'axios'
import type {
  Account,
  AccountEvent,
  AccountInvoice,
  AccountRow,
  Balance,
  BillingMode,
  Customer,
  CustomerWithBalance,
  FinanceSummary,
  Group,
  GroupInvoice,
  GroupWithBalance,
  LedgerEntry,
  OnlineHistory,
  OnlineHistoryRange,
  ReportSummary,
  SyncStatus,
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
      window.location.assign('/login')
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
  settle: async (id: number) => (await api.post(`/api/groups/${id}/settle`)).data,
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
  updateRelationship: async (id: number, body: { customer_id?: number | null; group_id?: number | null; role?: 'primary' | 'sub' }) =>
    (await api.patch<Account>(`/api/accounts/${id}/relationship`, body)).data,
  updateBilling: async (id: number, body: { rate_per_gb?: number | null; billing_mode?: BillingMode; clear_rate?: boolean }) =>
    (await api.patch<Account>(`/api/accounts/${id}/billing`, body)).data,
  adjust: async (
    id: number,
    body: { extend_days?: number; extend_gb?: number; set_expire?: number; set_data_limit_gb?: number; note?: string },
  ) => (await api.post<Account>(`/api/accounts/${id}/adjust`, body)).data,
  reset: async (id: number, body: { charge_amount?: number; note?: string }) =>
    (await api.post<Account>(`/api/accounts/${id}/reset`, body)).data,
  invoice: async (id: number) => (await api.get<AccountInvoice>(`/api/accounts/${id}/invoice`)).data,
  settle: async (id: number) => (await api.post(`/api/accounts/${id}/settle`)).data,
  events: async (id: number) => (await api.get<AccountEvent[]>(`/api/accounts/${id}/events`)).data,
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
  balance: async (params: { customer_id?: number; group_id?: number }) =>
    (await api.get<Balance>('/api/ledger/balance', { params })).data,
}

// ---- reports / sync ----
export const reportsApi = {
  summary: async () => (await api.get<ReportSummary>('/api/reports/summary')).data,
  finance: async () => (await api.get<FinanceSummary>('/api/reports/finance')).data,
  onlineHistory: async (range: OnlineHistoryRange) =>
    (await api.get<OnlineHistory>('/api/reports/online-history', { params: { range } })).data,
}

export const syncApi = {
  run: async () => (await api.post('/api/sync/run')).data,
  status: async () => (await api.get<SyncStatus>('/api/sync/status')).data,
}

// ---- settings ----
export const settingsApi = {
  get: async () => (await api.get<{ default_rate_per_gb: number | null }>('/api/settings')).data,
  update: async (body: { default_rate_per_gb: number | null }) =>
    (await api.patch<{ default_rate_per_gb: number | null }>('/api/settings', body)).data,
}
