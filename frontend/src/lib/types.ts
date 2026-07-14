export type AccountRole = 'primary' | 'sub'
export type LedgerType = 'charge' | 'credit'
export type LedgerSource = 'web' | 'bot' | 'sync'
export type BillingMode = 'prepay' | 'payg'

export interface Customer {
  id: number
  name: string
  contact: string | null
  is_group_rep: boolean
  created_at: string
}

export interface CustomerWithBalance extends Customer {
  balance: number
  account_count: number
}

export interface Group {
  id: number
  name: string
  representative_customer_id: number
  billing_cycle_days: number
  rate_per_gb: number | null
  last_settled_at: string | null
  created_at: string
}

export interface GroupWithBalance extends Group {
  balance: number
  account_count: number
  total_used_traffic: number
}

export interface Account {
  id: number
  marzban_username: string
  customer_id: number | null
  group_id: number | null
  role: AccountRole
  rate_per_gb: number | null
  billing_mode: BillingMode
  used_traffic: number
  lifetime_used_traffic: number
  data_limit: number | null
  expire: number | null
  status: string | null
  last_synced_at: string | null
  created_at: string
}

export interface AccountInvoice {
  account_id: number
  since: string | null
  billable_gb: number
  rate_per_gb: number
  amount: number
}

export interface LedgerEntry {
  id: number
  type: LedgerType
  amount: number
  date: string
  customer_id: number | null
  group_id: number | null
  account_id: number | null
  note: string | null
  source: LedgerSource
}

export interface Balance {
  entity_type: 'customer' | 'group'
  entity_id: number
  total_charge: number
  total_credit: number
  balance: number
}

export interface InvoiceLine {
  account_id: number
  marzban_username: string
  billable_gb: number
  rate_per_gb: number
  amount: number
}

export interface GroupInvoice {
  group_id: number
  rate_per_gb: number
  cycle_started_at: string | null
  lines: InvoiceLine[]
  total_amount: number
}

export interface ReportSummary {
  overdue_customers: { customer_id: number; name: string; balance: number }[]
  near_quota_accounts: { account_id: number; marzban_username: string; used_pct: number }[]
  near_expiry_accounts: { account_id: number; marzban_username: string; days_left: number }[]
  unassigned_accounts: { account_id: number; marzban_username: string }[]
  total_accounts: number
  total_customers: number
}

export interface FinanceTransaction {
  id: number
  type: LedgerType
  amount: number
  date: string
  note: string | null
  customer_name: string | null
  group_name: string | null
}

export interface RateOverviewRow {
  account_id: number
  marzban_username: string
  customer_name: string | null
  group_name: string | null
  rate_per_gb: number | null
  billing_mode: BillingMode
  effective_rate_source: 'account' | 'group' | null
}

export interface FinanceSummary {
  total_outstanding: number
  total_credit_balance: number
  revenue_this_month: number
  charged_this_month: number
  revenue_by_day: { date: string; amount: number }[]
  recent_transactions: FinanceTransaction[]
  rate_overview: RateOverviewRow[]
}
