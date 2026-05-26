// Theses filter bar — status + classification chips (ALP-678).
// Renders a row of toggle chips for status and classification filters.

import type { ThesesFilters } from '@/api/portfolio'

type Props = {
  filters: ThesesFilters
  onFiltersChange: (f: ThesesFilters) => void
}

const STATUS_OPTIONS = ['ACTIVE', 'RESOLVED', 'CANCELLED'] as const
const CLASSIFICATION_OPTIONS = [
  'ON_TRACK',
  'PARTIALLY_REALIZED',
  'AT_RISK',
  'STALE',
  'INVALIDATED',
] as const

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}): React.JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        'rounded border px-2 py-0.5 text-xs transition-colors',
        active
          ? 'border-primary bg-primary text-primary-foreground'
          : 'border-border hover:bg-muted text-muted-foreground',
      ].join(' ')}
    >
      {label}
    </button>
  )
}

export function ThesesFilterBar({ filters, onFiltersChange }: Props): React.JSX.Element {
  function toggleStatus(s: string) {
    onFiltersChange({ ...filters, status: filters.status === s ? undefined : s, page: 1 })
  }

  function toggleClassification(c: string) {
    onFiltersChange({
      ...filters,
      classification: filters.classification === c ? undefined : c,
      page: 1,
    })
  }

  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="text-muted-foreground text-xs font-medium">Status</span>
      {STATUS_OPTIONS.map((s) => (
        <FilterChip
          key={s}
          label={s}
          active={filters.status === s}
          onClick={() => {
            toggleStatus(s)
          }}
        />
      ))}
      <span className="text-muted-foreground ml-2 text-xs font-medium">Classification</span>
      {CLASSIFICATION_OPTIONS.map((c) => (
        <FilterChip
          key={c}
          label={c.replaceAll('_', ' ')}
          active={filters.classification === c}
          onClick={() => {
            toggleClassification(c)
          }}
        />
      ))}
    </div>
  )
}
