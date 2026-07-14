import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

/** State chips — tinted, never solid-filled: a saturated pill per row turns a
 * table into confetti. Badges carry STATE WORDS (active, due, unassigned…);
 * money amounts are never put inside one (see <Money/>). */
const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-md border px-1.5 py-px text-[11px] font-medium leading-[18px] whitespace-nowrap',
  {
    variants: {
      variant: {
        default: 'border-primary/20 bg-primary/10 text-primary',
        secondary: 'border-transparent bg-muted text-muted-foreground',
        destructive: 'border-destructive/20 bg-destructive/10 text-destructive',
        success: 'border-success/25 bg-success/10 text-success',
        warning: 'border-warning/25 bg-warning/10 text-warning',
        credit: 'border-credit/25 bg-credit/10 text-credit',
        outline: 'border-border bg-transparent text-muted-foreground',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  },
)

export interface BadgeProps extends React.HTMLAttributes<HTMLDivElement>, VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <div className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
