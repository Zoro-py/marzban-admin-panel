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

// Unicode bidi isolate controls (UAX #9 — https://unicode.org/reports/tr9):
// RIGHT-TO-LEFT ISOLATE (U+2067), LEFT-TO-RIGHT ISOLATE (U+2066), POP
// DIRECTIONAL ISOLATE (U+2069). RLI/LRI open an isolate that forces
// everything up to the matching PDI to be treated as one unit of the given
// direction, without letting it leak into (or be disrupted by) the
// surrounding text. These three constants hold the literal (invisible)
// control characters themselves, confirmed byte-for-byte against Unicode's
// own TR9 reference before use here — if a diff ever shows one of these
// lines as "changed" with no visible content difference, that's this
// character being touched; verify the codepoint before assuming it's noise.
const RLI = '⁧' // RIGHT-TO-LEFT ISOLATE
const LRI = '⁦' // LEFT-TO-RIGHT ISOLATE
const PDI = '⁩' // POP DIRECTIONAL ISOLATE

/** Forces a line of plain text (no HTML/CSS available — this is for text
 * pasted into a chat app) to read as RTL overall, even when it starts with
 * Latin content.
 *
 * Per UAX #9 rule P2/P3, a paragraph's base direction is set by its FIRST
 * STRONG-directional character — bullets, spaces, digits and punctuation are
 * neutral and get skipped over. A Persian invoice line like "• R_boojar:
 * ۶۲۷۹ تومان" has "R" as that first strong character, so the whole line
 * flips to an LTR base: everything after it, including the Persian amount
 * and any parentheses, gets reflowed to fit an LTR reading order, which is
 * what actually made the message unreadable — not the Persian text itself,
 * and not any single character being wrong, but every name-first line
 * silently disagreeing with the Persian-first lines around it (the title and
 * total, which already start with a Persian word and were never affected).
 *
 * Wrapping the whole line in RLI…PDI fixes the paragraph direction outright,
 * independent of what character happens to come first. Isolates (not RLM,
 * not the older RLE/PDF embeddings) are the correct tool here specifically
 * because this function gets called once per line in a loop — an isolate
 * can't leak into the next call the way an unclosed embedding could. */
export function bidiRtlLine(text: string): string {
  return `${RLI}${text}${PDI}`
}

/** Marks a run of Latin text (a username) as an isolated LTR island inside
 * an RTL line. Without this, neutral characters immediately touching it
 * (the colon after a name, the bullet before it) can attach to the wrong
 * side once the surrounding isolate is RTL — isolating the name itself
 * keeps it a clean, self-contained unit no matter what's next to it. */
export function bidiLtrSpan(text: string): string {
  return `${LRI}${text}${PDI}`
}
