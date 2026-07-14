import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

const GB = 1024 ** 3

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '∞' // unlimited
  if (bytes === 0) return '0 GB'
  return `${(bytes / GB).toFixed(2)} GB`
}

export function formatToman(amount: number): string {
  const sign = amount < 0 ? '-' : ''
  return `${sign}${Math.abs(Math.round(amount)).toLocaleString('en-US')} T`
}

/** Backend datetimes are naive UTC (SQLite strips tzinfo; FastAPI serializes
 * them without a Z) — parsing those as LOCAL time skews everything by the
 * operator's UTC offset ("synced just now" reads as "3h ago" in Tehran).
 * Treat a timestamp string with no timezone marker as UTC. */
function parseDate(value: string | number): Date {
  if (typeof value === 'number') return new Date(value * 1000)
  if (value.includes(':') && !/Z$|[+-]\d{2}:?\d{2}$/.test(value)) {
    return new Date(value.replace(' ', 'T') + 'Z')
  }
  return new Date(value)
}

export function formatDate(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const date = parseDate(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
}

export function formatDateTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const date = parseDate(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function daysUntil(unixSeconds: number | null | undefined): number | null {
  if (unixSeconds === null || unixSeconds === undefined) return null
  return Math.round((unixSeconds * 1000 - Date.now()) / (1000 * 60 * 60 * 24))
}

/** "just now" / "4m ago" / "2h ago" / "3d ago" — for sync freshness and
 * history timestamps, where "how long ago" is the question, not the date. */
export function formatAgo(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const date = parseDate(value)
  if (Number.isNaN(date.getTime())) return '—'
  const s = Math.max(0, (Date.now() - date.getTime()) / 1000)
  if (s < 60) return 'just now'
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`
  return formatDate(typeof value === 'number' ? value : String(value))
}

export function usagePct(used: number, limit: number | null | undefined): number | null {
  if (!limit) return null
  return (used / limit) * 100
}
