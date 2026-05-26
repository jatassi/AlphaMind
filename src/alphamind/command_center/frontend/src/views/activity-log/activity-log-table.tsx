// TanStack Table over the activity-log endpoint.
//
// Columns: event_type, entry_at, primary entity link, source, one-line summary.
// Row expansion renders the full <DetailPanel />. The parent drives filter state
// and pagination via props; the table renders the current page only.

import { useState } from 'react'

import type { Row } from '@tanstack/react-table'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table'

import type { ActivityLogRow } from '@/api/queries'

import { DetailPanel } from './detail-panel'

// ── Column helpers ──────────────────────────────────────────────────────────

const col = createColumnHelper<ActivityLogRow>()

function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function primaryEntityLabel(row: ActivityLogRow): string {
  if (row.position_id) {
    return `pos:${row.position_id}`
  }
  if (row.order_id) {
    return `ord:${row.order_id}`
  }
  if (row.thesis_id) {
    return `the:${row.thesis_id}`
  }
  return '—'
}

function extractSummary(detail: Record<string, unknown>): string | null {
  const candidate = detail.description ?? detail.reason ?? detail.verdict ?? null
  return typeof candidate === 'string' && candidate ? candidate.slice(0, 80) : null
}

function summaryLine(row: ActivityLogRow): string {
  try {
    const parsed = JSON.parse(row.detail_json) as unknown
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      return extractSummary(parsed as Record<string, unknown>) ?? row.event_type
    }
    return row.event_type
  } catch {
    return row.event_type
  }
}

const COLUMNS = [
  col.accessor('event_type', {
    header: 'Event type',
    cell: (info) => <span className="font-mono text-xs">{info.getValue()}</span>,
  }),
  col.accessor('entry_at', {
    header: 'Timestamp',
    cell: (info) => (
      <span className="text-xs whitespace-nowrap">{formatTimestamp(info.getValue())}</span>
    ),
  }),
  col.display({
    id: 'entity',
    header: 'Entity',
    cell: ({ row }) => (
      <span className="font-mono text-xs">{primaryEntityLabel(row.original)}</span>
    ),
  }),
  col.accessor('source', {
    header: 'Source',
    cell: (info) => <span className="text-muted-foreground text-xs">{info.getValue()}</span>,
  }),
  col.display({
    id: 'summary',
    header: 'Summary',
    cell: ({ row }) => <span className="text-xs">{summaryLine(row.original)}</span>,
  }),
]

// ── Expandable row ──────────────────────────────────────────────────────────

type ExpandableRowProps = {
  row: Row<ActivityLogRow>
  isExpanded: boolean
  onToggle: () => void
}

function ExpandableRow({ row, isExpanded, onToggle }: ExpandableRowProps): React.JSX.Element {
  const colSpan = row.getVisibleCells().length

  return (
    <>
      <tr
        className="hover:bg-muted/40 cursor-pointer"
        onClick={onToggle}
        aria-expanded={isExpanded}
      >
        {row.getVisibleCells().map((cell) => (
          <td key={cell.id} className="px-3 py-2">
            {flexRender(cell.column.columnDef.cell, cell.getContext())}
          </td>
        ))}
      </tr>
      {isExpanded ? (
        <tr>
          <td colSpan={colSpan} className="bg-muted/20">
            <DetailPanel row={row.original} />
          </td>
        </tr>
      ) : null}
    </>
  )
}

// ── Table component ─────────────────────────────────────────────────────────

type TableBodyProps = {
  rows: Row<ActivityLogRow>[]
  expandedId: string | null
  onToggle: (id: string) => void
}

function TableBody({ rows, expandedId, onToggle }: TableBodyProps): React.JSX.Element {
  return (
    <tbody className="divide-border divide-y">
      {rows.map((row) => (
        <ExpandableRow
          key={row.id}
          row={row}
          isExpanded={expandedId === row.original.entry_id}
          onToggle={() => onToggle(row.original.entry_id)}
        />
      ))}
    </tbody>
  )
}

type Props = {
  rows: ActivityLogRow[]
  isLoading: boolean
}

export function ActivityLogTable({ rows, isLoading }: Props): React.JSX.Element {
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const table = useReactTable({
    data: rows,
    columns: COLUMNS,
    getCoreRowModel: getCoreRowModel(),
  })

  if (isLoading) {
    return <p className="text-muted-foreground py-8 text-center text-sm">Loading…</p>
  }

  if (rows.length === 0) {
    return (
      <p className="text-muted-foreground py-8 text-center text-sm">
        No rows match the current filters.
      </p>
    )
  }

  const toggleRow = (id: string): void => {
    setExpandedId(expandedId === id ? null : id)
  }

  return (
    <div className="border-border overflow-x-auto rounded border">
      <table className="divide-border min-w-full divide-y text-sm">
        <thead className="bg-muted/50">
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((header) => (
                <th
                  key={header.id}
                  className="px-3 py-2 text-left text-xs font-medium tracking-wide uppercase"
                >
                  {flexRender(header.column.columnDef.header, header.getContext())}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <TableBody rows={table.getRowModel().rows} expandedId={expandedId} onToggle={toggleRow} />
      </table>
    </div>
  )
}
