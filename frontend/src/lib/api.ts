import axios, { AxiosError } from 'axios'
import type {
  Account,
  Balance,
  Customer,
  CustomerWithBalance,
  Group,
  GroupInvoice,
  GroupWithBalance,
  LedgerEntry,
  ReportSummary,
} from './types'

const TOKEN_KEY = 'vpn_dashboard_token'

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000',
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
export async function login(username: string, password: string): Promise<string> {
  const { data } = await api.post<{ access_token: string }>('/api/auth/login', { username, password })
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
  accounts: async (id: number) => (await api.get<Account[]>(`/api/customers/${id}/accounts`)).data,
}

// ---- groups ----
export const groupsApi = {
  list: async () => (await api.get<GroupWithBalance[]>('/api/groups')).data,
  get: async (id: number) => (await api.get<GroupWithBalance>(`/api/groups/${id}`)).data,
  create: async (body: { name: string; representative_customer_id: number; billing_cycle_days?: number; rate_per_gb?: number }) =>
    (await api.post<Group>('/api/groups', body)).data,
  update: async (id: number, body: Partial<{ name: string; billing_cycle_days: number; rate_per_gb: number }>) =>
    (await api.patch<Group>(`/api/groups/${id}`, body)).data,
  accounts: async (id: number) => (await api.get<Account[]>(`/api/groups/${id}/accounts`)).data,
  invoice: async (id: number) => (await api.get<GroupInvoice>(`/api/groups/${id}/invoice`)).data,
  settle: async (id: number) => (await api.post(`/api/groups/${id}/settle`)).data,
}

// ---- accounts ----
export const accountsApi = {
  list: async (params?: { unassigned_only?: boolean; customer_id?: number; group_id?: number }) =>
    (await api.get<Account[]>('/api/accounts', { params })).data,
  get: async (id: number) => (await api.get<Account>(`/api/accounts/${id}`)).data,
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
  adjust: async (
    id: number,
    body: { extend_days?: number; extend_gb?: number; set_expire?: number; set_data_limit_gb?: number; note?: string },
  ) => (await api.post<Account>(`/api/accounts/${id}/adjust`, body)).data,
  invoice: async (id: number) => (await api.get(`/api/accounts/${id}/invoice`)).data,
  settle: async (id: number) => (await api.post(`/api/accounts/${id}/settle`)).data,
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
}

export const syncApi = {
  run: async () => (await api.post('/api/sync/run')).data,
}
