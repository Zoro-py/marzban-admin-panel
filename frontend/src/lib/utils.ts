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

export function formatDate(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
}

export function daysUntil(unixSeconds: number | null | undefined): number | null {
  if (unixSeconds === null || unixSeconds === undefined) return null
  return Math.round((unixSeconds * 1000 - Date.now()) / (1000 * 60 * 60 * 24))
}
