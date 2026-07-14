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
}

/** THE way money renders in this app: plain tabular text, colored by meaning,
 * never inside a filled badge. Keeps debt / credit / pending / settled
 * visually distinct everywhere with one component instead of per-page ad-hoc
 * badge choices. */
export function Money({ amount, kind = 'balance', zero = 'dash', className }: MoneyProps) {
  if (kind === 'plain') {
    return <span className={cn('tabular-nums', className)}>{formatToman(amount)}</span>
  }

  if (kind === 'pending') {
    if (amount <= 0) return <span className={cn('text-muted-foreground', className)}>—</span>
    return <span className={cn('tabular-nums font-medium text-warning', className)}>{formatToman(amount)}</span>
  }

  // balance
  if (amount === 0) {
    return zero === 'settled' ? (
      <span className={cn('text-muted-foreground', className)}>settled</span>
    ) : (
      <span className={cn('text-muted-foreground', className)}>—</span>
    )
  }
  if (amount > 0) {
    return <span className={cn('tabular-nums font-medium text-destructive', className)}>{formatToman(amount)}</span>
  }
  return <span className={cn('tabular-nums font-medium text-credit', className)}>{formatToman(Math.abs(amount))} cr</span>
}
