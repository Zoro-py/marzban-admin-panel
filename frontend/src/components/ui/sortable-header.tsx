import { ArrowDown, ArrowUp, ChevronsUpDown } from 'lucide-react'
import { TableHead } from '@/components/ui/table'
import { cn } from '@/lib/utils'

export interface SortState {
  key: string
  dir: 'asc' | 'desc'
}

/** Cycles asc -> desc -> off on each click, per item 5 of the UI ask
 * ("clickable/cyclable column-header sorting"). Off returns to the caller's
 * default (usually creation order), not a frozen last-sorted state. */
export function nextSort(current: SortState | null, key: string): SortState | null {
  if (current?.key !== key) return { key, dir: 'asc' }
  if (current.dir === 'asc') return { key, dir: 'desc' }
  return null
}

interface SortableHeaderProps {
  label: string
  sortKey: string
  sort: SortState | null
  onSort: (key: string) => void
  className?: string
  align?: 'left' | 'right'
}

export function SortableHeader({ label, sortKey, sort, onSort, className, align = 'left' }: SortableHeaderProps) {
  const active = sort?.key === sortKey
  const Icon = active ? (sort!.dir === 'asc' ? ArrowUp : ArrowDown) : ChevronsUpDown

  return (
    <TableHead className={className}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={cn(
          'inline-flex items-center gap-1 rounded text-xs font-medium uppercase tracking-wide text-muted-foreground transition-colors hover:text-foreground',
          active && 'text-foreground',
          align === 'right' && 'flex-row-reverse',
        )}
      >
        {label}
        <Icon className={cn('h-3.5 w-3.5', !active && 'opacity-40')} />
      </button>
    </TableHead>
  )
}
