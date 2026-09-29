import { cn, formatToman } from '@/lib/utils'

interface MoneyProps {
  amount: number
  /** balance: sign carries meaning (positive = they owe us → danger red;
   *  negative = credit owed back to them → violet; zero → quiet).
   *  pending: not-yet-charged amount → amber when nonzero.
   *  plain: neutral figure. */
  kind?: 'balance' | 'pending' | 'plain'
  /** What to render for a zero balance: an em-dash (tables) or the word
   * "settled" (detail headers). */
  zero?: 'dash' | 'settled'
  className?: string
  /** Optional tooltip (e.g. the window caption when a list-wide ?since=
   * picker is active). Purely presentational. */
  title?: string
}

/** THE way money renders in this app: plain tabular text, colored by meaning,
 * never inside a filled badge. Keeps debt / credit / pending / settled
 * visually distinct everywhere with one component instead of per-page ad-hoc
 * badge choices. */
export function Money({ amount, kind = 'balance', zero = 'dash', className, title }: MoneyProps) {
  if (kind === 'plain') {
    return <span className={cn('tabular-nums', className)} title={title}>{formatToman(amount)}</span>
  }

  if (kind === 'pending') {
    if (amount <= 0) return <span className={cn('text-muted-foreground', className)}>—</span>
    return <span className={cn('tabular-nums font-medium text-warning', className)} title={title}>{formatToman(amount)}</span>
  }

  // balance
  if (amount === 0) {
    return zero === 'settled' ? (
      <span className={cn('text-muted-foreground', className)} title={title}>settled</span>
    ) : (
      <span className={cn('text-muted-foreground', className)} title={title}>—</span>
    )
  }
  if (amount > 0) {
    return <span className={cn('tabular-nums font-medium text-destructive', className)} title={title}>{formatToman(amount)}</span>
  }
  return <span className={cn('tabular-nums font-medium text-credit', className)} title={title}>{formatToman(Math.abs(amount))} cr</span>
}
