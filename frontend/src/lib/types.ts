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
  // Groups this customer is the billing representative for (computed
  // server-side from Group rows, not the manual is_group_rep flag).
  represented_group_names: string[]
}

export interface Group {
  id: number
  name: string
  representative_customer_id: number
  billing_cycle_days: number
  rate_per_gb: number | null
  billing_mode: BillingMode
  last_settled_at: string | null
  created_at: string
}

export interface GroupWithBalance extends Group {
  balance: number
  account_count: number
  // Marzban's own used_traffic counter, summed — NOT what drives billing.
  total_used_traffic: number
  // Usage accrued since the group's last settlement — what a settle right
  // now would actually charge. This is the figure to show as "current usage."
  current_cycle_used_bytes: number
  // What settling right now would charge, at each member's effective rate.
  pending_amount: number
  next_due_at: string
  is_due: boolean
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

export type UsageConfidence = 'insufficient_data' | 'preliminary' | 'full'

export interface AccountRow extends Account {
  customer_name: string | null
  group_name: string | null
  effective_rate: number
  // Whether effective_rate came from an actual configured value somewhere in
  // the chain, vs. falling through to 0 because nothing was ever set — an
  // explicit 0 (comp/free account) is rate_configured=true, distinct from
  // never-configured.
  rate_configured: boolean
  payer_balance: number
  // This account's own unbilled usage-based amount since its last settle —
  // always 0 for prepay. payer_balance alone stays 0 for a grouped account
  // until the whole group is settled, so this is what makes a member with
  // real usage show as owing something before that happens.
  pending_amount: number
  // How this account is ACTUALLY billed: its group's mode when grouped
  // (group settle bills every member by the group's mode regardless of their
  // own field), else its own billing_mode. Use this, not the raw
  // `billing_mode` field, for anything deciding "is this payg."
  effective_billing_mode: BillingMode
  monthly_avg_usage_gb: number | null
  usage_confidence: UsageConfidence
  usage_sample_days: number
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
  exhausted_accounts: { account_id: number; marzban_username: string; used_pct: number; owner_name: string | null }[]
  near_quota_accounts: { account_id: number; marzban_username: string; used_pct: number; owner_name: string | null }[]
  expired_accounts: { account_id: number; marzban_username: string; days_left: number; owner_name: string | null }[]
  near_expiry_accounts: { account_id: number; marzban_username: string; days_left: number; owner_name: string | null }[]
  no_rate_accounts: { account_id: number; marzban_username: string; owner_name: string | null }[]
  unassigned_accounts: { account_id: number; marzban_username: string }[]
  pending_settlement: {
    type: 'group' | 'account'
    id: number
    name: string
    billing_mode: BillingMode
    // Unbilled usage-based preview since the last settle (payg groups/accounts
    // only — always 0 for prepay, which has no usage-driven cycle).
    pending_amount: number
    // Real posted debt (charges minus credits). Populated for groups (both
    // prepay and payg — group-scoped balance never overlaps another entity's).
    // Always 0 for standalone accounts: their debt is customer-level and
    // already surfaced via overdue_customers, so repeating it here per-account
    // would double-count the same debt across every account a customer owns.
    balance: number
    is_due: boolean | null
    days_overdue: number | null
  }[]
  total_pending: number
  total_accounts: number
  total_customers: number
}

export interface AccountEvent {
  id: number
  account_id: number
  action: string
  detail: string
  date: string
  source: LedgerSource
}

export interface SyncStatus {
  last_synced_at: string | null
  account_count: number
}

export type OnlineHistoryRange = '1d' | '3d' | '1w' | '1m'

export interface OnlineHistoryPoint {
  recorded_at: string
  online_count: number
  total_accounts: number
}

export interface OnlineHistory {
  range: OnlineHistoryRange
  points: OnlineHistoryPoint[]
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
  rate_per_gb: number
  rate_configured: boolean
  billing_mode: BillingMode
  effective_rate_source: 'account' | 'group' | 'default'
}

export interface FinanceSummary {
  total_outstanding: number
  total_credit_balance: number
  revenue_this_month: number
  charged_this_month: number
  revenue_by_day: { date: string; amount: number }[]
  charged_by_day: { date: string; amount: number }[]
  recent_transactions: FinanceTransaction[]
  rate_overview: RateOverviewRow[]
}
