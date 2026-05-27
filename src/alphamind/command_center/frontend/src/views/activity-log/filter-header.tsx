// Filter header for the activity-log explorer.
//
// Renders chip groups for event_type + source, free-text inputs for entity IDs,
// and date-time pickers for time_from / time_to. Each filter change resets the
// page to 1 via the onChange callback.

import { useEventTypes } from '@/api/queries'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

import { ChipGroup } from './filter-chips'
import type { FilterState } from './filter-state'
import { toggleInList } from './filter-state'

// Hardcoded from EventSource in the backend — stable enum values.
const SOURCE_OPTIONS = [
  'FILL_PROCESSOR',
  'COMMAND_EXECUTOR',
  'BRACKET_MANAGER',
  'MARGIN_MONITOR',
  'GUARDRAIL_LAYER',
  'CORPORATE_ACTION_PROCESSOR',
  'CONFIG_RELOAD',
  'OPERATOR_CONSOLE',
]

type TextFieldProps = {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
}

function TextField({ id, label, value, onChange }: TextFieldProps): React.JSX.Element {
  return (
    <div className="space-y-1">
      <Label htmlFor={id} className="text-xs">
        {label}
      </Label>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-8 text-xs"
        placeholder={`Filter by ${label.toLowerCase()}`}
      />
    </div>
  )
}

type DateRangeProps = {
  timeFrom: string
  timeTo: string
  onChange: (patch: Partial<FilterState>) => void
}

function DateRangeInputs({ timeFrom, timeTo, onChange }: DateRangeProps): React.JSX.Element {
  return (
    <div className="grid grid-cols-2 gap-3">
      <div className="space-y-1">
        <Label htmlFor="filter-time-from" className="text-xs">
          From
        </Label>
        <Input
          id="filter-time-from"
          type="datetime-local"
          value={timeFrom}
          onChange={(e) => onChange({ time_from: e.target.value, page: 1 })}
          className="h-8 text-xs"
        />
      </div>
      <div className="space-y-1">
        <Label htmlFor="filter-time-to" className="text-xs">
          To
        </Label>
        <Input
          id="filter-time-to"
          type="datetime-local"
          value={timeTo}
          onChange={(e) => onChange({ time_to: e.target.value, page: 1 })}
          className="h-8 text-xs"
        />
      </div>
    </div>
  )
}

type Props = {
  filters: FilterState
  onChange: (update: Partial<FilterState>) => void
}

export function FilterHeader({ filters, onChange }: Props): React.JSX.Element {
  const eventTypesQuery = useEventTypes()
  const eventTypes = eventTypesQuery.data?.event_types ?? []

  return (
    <div className="border-border space-y-4 rounded border p-4">
      <ChipGroup
        label="Event type"
        options={eventTypes}
        selected={filters.event_type}
        onToggle={(v) => onChange({ event_type: toggleInList(filters.event_type, v), page: 1 })}
      />

      <ChipGroup
        label="Source"
        options={SOURCE_OPTIONS}
        selected={filters.source}
        onToggle={(v) => onChange({ source: toggleInList(filters.source, v), page: 1 })}
      />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <TextField
          id="filter-invocation-id"
          label="Invocation ID"
          value={filters.invocation_id}
          onChange={(v) => onChange({ invocation_id: v, page: 1 })}
        />
        <TextField
          id="filter-position-id"
          label="Position ID"
          value={filters.position_id}
          onChange={(v) => onChange({ position_id: v, page: 1 })}
        />
        <TextField
          id="filter-thesis-id"
          label="Thesis ID"
          value={filters.thesis_id}
          onChange={(v) => onChange({ thesis_id: v, page: 1 })}
        />
        <TextField
          id="filter-order-id"
          label="Order ID"
          value={filters.order_id}
          onChange={(v) => onChange({ order_id: v, page: 1 })}
        />
      </div>

      <DateRangeInputs timeFrom={filters.time_from} timeTo={filters.time_to} onChange={onChange} />
    </div>
  )
}
