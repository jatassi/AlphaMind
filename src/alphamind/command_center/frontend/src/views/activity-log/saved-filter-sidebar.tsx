// Saved-filter sidebar — lists the built-in saved-filter presets and applies
// them when clicked.
//
// v1 ships one preset: "Operator actions" (source=operator_console). The
// sidebar reads from GET /api/views/activity-log/saved-filters and calls
// `onApply` with the preset's params so the parent can merge them into the
// active filter state.

import { type SavedFilter, useSavedFilters } from '@/api/queries'

type PresetCardProps = {
  preset: SavedFilter
  onApply: (preset: SavedFilter) => void
}

function PresetCard({ preset, onApply }: PresetCardProps): React.JSX.Element {
  return (
    <button
      type="button"
      className="border-border hover:bg-muted w-full rounded border p-3 text-left"
      onClick={() => onApply(preset)}
    >
      <p className="text-sm font-medium">{preset.name}</p>
      <p className="text-muted-foreground text-xs">{preset.description}</p>
    </button>
  )
}

type Props = {
  onApply: (preset: SavedFilter) => void
}

export function SavedFilterSidebar({ onApply }: Props): React.JSX.Element {
  const query = useSavedFilters()

  return (
    <aside className="w-52 shrink-0 space-y-2">
      <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
        Saved filters
      </p>
      {query.isPending ? <p className="text-muted-foreground text-xs">Loading…</p> : null}
      {query.isError ? <p className="text-xs text-red-500">Failed to load presets</p> : null}
      {query.data?.saved_filters.map((preset) => (
        <PresetCard key={preset.name} preset={preset} onApply={onApply} />
      ))}
    </aside>
  )
}
