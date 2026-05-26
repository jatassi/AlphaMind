// Multi-select chip controls for event_type and source filters.
//
// Each chip toggles its value in the multi-select list. Active chips are
// visually highlighted. The chip bar scrolls horizontally when the items
// overflow the container width.

import { cn } from '@/lib/utils'

type ChipProps = {
  label: string
  active: boolean
  onClick: () => void
}

function Chip({ label, active, onClick }: ChipProps): React.JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'inline-flex shrink-0 cursor-pointer items-center rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors',
        active
          ? 'border-primary bg-primary text-primary-foreground'
          : 'border-border bg-background text-foreground hover:bg-muted',
      )}
    >
      {label}
    </button>
  )
}

type ChipGroupProps = {
  label: string
  options: string[]
  selected: string[]
  onToggle: (value: string) => void
}

export function ChipGroup({
  label,
  options,
  selected,
  onToggle,
}: ChipGroupProps): React.JSX.Element {
  return (
    <div className="space-y-1">
      <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">{label}</p>
      <div className="flex flex-wrap gap-1">
        {options.map((opt) => (
          <Chip
            key={opt}
            label={opt}
            active={selected.includes(opt)}
            onClick={() => onToggle(opt)}
          />
        ))}
      </div>
    </div>
  )
}
