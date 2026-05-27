import { useNavigate } from '@tanstack/react-router'
import { type ColumnDef, flexRender, getCoreRowModel, useReactTable } from '@tanstack/react-table'

import type { RunHistoryFilters, RunRow } from './types'

// Column definitions for the run history TanStack Table.
//
// Columns per design doc:
//   invocation_id, started_at, duration, run_type, status,
//   # commands, # rejections, abort_reason.

const STATUS_CLS: Record<string, string> = {
  completed: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
  partial: 'bg-yellow-100 text-yellow-800',
}

function StatusBadge({ status }: { status: string }): React.JSX.Element {
  const cls = STATUS_CLS[status] ?? 'bg-muted text-muted-foreground'
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${cls}`}>{status}</span>
}

function DurationCell({ secs }: { secs: number | null }): React.JSX.Element {
  if (secs === null) {
    return <span className="text-muted-foreground">—</span>
  }
  const mins = Math.floor(secs / 60)
  const rem = Math.round(secs % 60)
  const label = mins > 0 ? `${String(mins)}m ${String(rem)}s` : `${String(rem)}s`
  return <span className="text-sm">{label}</span>
}

const COLUMNS: ColumnDef<RunRow>[] = [
  {
    accessorKey: 'invocation_id',
    header: 'Invocation ID',
    cell: (info) => <span className="font-mono text-xs">{String(info.getValue())}</span>,
  },
  {
    accessorKey: 'started_at',
    header: 'Started at',
    cell: (info) => (
      <span className="text-sm">{(info.getValue() as string).replace('T', ' ')}</span>
    ),
  },
  {
    accessorKey: 'duration_seconds',
    header: 'Duration',
    cell: (info) => <DurationCell secs={info.getValue() as number | null} />,
  },
  {
    accessorKey: 'run_type',
    header: 'Run type',
    cell: (info) => <span className="font-mono text-xs">{String(info.getValue())}</span>,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: (info) => <StatusBadge status={String(info.getValue())} />,
  },
  {
    accessorKey: 'commands_submitted',
    header: '# Commands',
    cell: (info) => <span className="text-sm">{String(info.getValue())}</span>,
  },
  {
    accessorKey: 'commands_rejected',
    header: '# Rejections',
    cell: (info) => {
      const n = info.getValue() as number
      return (
        <span className={`text-sm ${n > 0 ? 'font-semibold text-red-600' : ''}`}>{String(n)}</span>
      )
    },
  },
  {
    accessorKey: 'abort_reason',
    header: 'Abort reason',
    cell: (info) => {
      const val = info.getValue() as string | null
      if (val !== null && val !== '') {
        return <span className="text-xs text-red-600">{val}</span>
      }
      return <span className="text-muted-foreground">—</span>
    },
  },
]

// ---------------------------------------------------------------------------
// Empty / loading states
// ---------------------------------------------------------------------------

function EmptyRow({
  colSpan,
  isLoading,
}: {
  colSpan: number
  isLoading: boolean
}): React.JSX.Element {
  const text = isLoading ? 'Loading…' : 'No runs match the current filters.'
  return (
    <tr>
      <td colSpan={colSpan} className="text-muted-foreground px-3 py-4 text-center">
        {text}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// RunList component
// ---------------------------------------------------------------------------

type RunListProps = {
  rows: RunRow[]
  isLoading: boolean
  isPreset?: boolean
  filters: RunHistoryFilters
  onFilterChange: (f: RunHistoryFilters) => void
}

export function RunList({
  rows,
  isLoading,
  isPreset = false,
  filters,
  onFilterChange,
}: RunListProps): React.JSX.Element {
  const navigate = useNavigate()

  const table = useReactTable<RunRow>({
    data: rows,
    columns: COLUMNS,
    getCoreRowModel: getCoreRowModel(),
  })

  const isEmpty = !isLoading && rows.length === 0

  return (
    <div className="space-y-4">
      {!isPreset && <RunFilters filters={filters} onFilterChange={onFilterChange} />}
      <div className="overflow-x-auto rounded border">
        <table className="w-full text-left text-sm">
          <thead className="bg-muted/40 border-b">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => (
                  <th key={h.id} className="px-3 py-2 font-medium">
                    {flexRender(h.column.columnDef.header, h.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {isLoading || isEmpty ? (
              <EmptyRow colSpan={COLUMNS.length} isLoading={isLoading} />
            ) : (
              table
                .getRowModel()
                .rows.map((row) => (
                  <DataRow key={row.id} row={row} navigate={navigate} colCount={COLUMNS.length} />
                ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// DataRow — extracted to keep RunList under 50 lines
// ---------------------------------------------------------------------------

type DataRowProps = {
  row: ReturnType<ReturnType<typeof useReactTable<RunRow>>['getRowModel']>['rows'][number]
  navigate: ReturnType<typeof useNavigate>
  colCount: number
}

function DataRow({ row, navigate }: DataRowProps): React.JSX.Element {
  return (
    <tr
      className="hover:bg-muted/30 cursor-pointer border-b"
      onClick={() => {
        // Navigate to the per-invocation detail page (story 05d / ALP-674).
        void navigate({
          to: '/history/$invocationId',
          params: { invocationId: row.original.invocation_id },
        })
      }}
    >
      {row.getVisibleCells().map((cell) => (
        <td key={cell.id} className="px-3 py-2">
          {flexRender(cell.column.columnDef.cell, cell.getContext())}
        </td>
      ))}
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Filter controls
// ---------------------------------------------------------------------------

const STATUS_OPTIONS = ['completed', 'failed', 'partial'] as const

type RunFiltersProps = {
  filters: RunHistoryFilters
  onFilterChange: (f: RunHistoryFilters) => void
}

function StatusChips({ filters, onFilterChange }: RunFiltersProps): React.JSX.Element {
  function toggle(status: string): void {
    const current = filters.status ?? []
    const next = current.includes(status)
      ? current.filter((s) => s !== status)
      : [...current, status]
    onFilterChange({ ...filters, status: next.length > 0 ? next : undefined })
  }

  return (
    <div className="flex items-center gap-2">
      <span className="font-medium">Status</span>
      {STATUS_OPTIONS.map((s) => {
        const active = (filters.status ?? []).includes(s)
        return (
          <button
            key={s}
            type="button"
            onClick={() => toggle(s)}
            className={`rounded px-2 py-0.5 text-xs ${active ? 'bg-primary text-primary-foreground' : 'border'}`}
          >
            {s}
          </button>
        )
      })}
    </div>
  )
}

export function RunFilters({ filters, onFilterChange }: RunFiltersProps): React.JSX.Element {
  const hasStatus = (filters.status ?? []).length > 0
  return (
    <div className="flex flex-wrap gap-4 text-sm">
      <div className="flex items-center gap-2">
        <label className="font-medium" htmlFor="date-from">
          From
        </label>
        <input
          id="date-from"
          type="date"
          value={filters.date_from ?? ''}
          onChange={(e) => onFilterChange({ ...filters, date_from: e.target.value || undefined })}
          className="rounded border px-2 py-1"
        />
      </div>
      <div className="flex items-center gap-2">
        <label className="font-medium" htmlFor="date-to">
          To
        </label>
        <input
          id="date-to"
          type="date"
          value={filters.date_to?.slice(0, 10) ?? ''}
          onChange={(e) =>
            onFilterChange({
              ...filters,
              date_to: e.target.value ? `${e.target.value}T23:59:59` : undefined,
            })
          }
          className="rounded border px-2 py-1"
        />
      </div>
      <StatusChips filters={filters} onFilterChange={onFilterChange} />
      {hasStatus ? (
        <button
          type="button"
          onClick={() => onFilterChange({ ...filters, status: undefined })}
          className="text-muted-foreground text-xs underline"
        >
          Clear filters
        </button>
      ) : null}
    </div>
  )
}
