// Theses table — one row per thesis (ALP-678).
// Built with TanStack Table. Columns per command-center.md § Theses dashboard.

import { Link } from '@tanstack/react-router'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table'

import type { ThesisRow } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { theses: ThesisRow[]; total: number }

const helper = createColumnHelper<ThesisRow>()

function fmtAge(hours: number): string {
  if (hours < 24) {
    return `${hours.toFixed(1)}h`
  }
  return `${(hours / 24).toFixed(1)}d`
}

function fmtUsd(val: string | null): string {
  if (val === null) {
    return '—'
  }
  const num = Number(val)
  if (!Number.isFinite(num)) {
    return val
  }
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(num)
}

function StatusBadge({ status }: { status: string }): React.JSX.Element {
  const colorMap: Record<string, string> = {
    ON_TRACK: 'text-green-600',
    PARTIALLY_REALIZED: 'text-yellow-600',
    AT_RISK: 'text-orange-600',
    STALE: 'text-gray-500',
    INVALIDATED: 'text-red-600',
  }
  const color = colorMap[status] ?? 'text-muted-foreground'
  return <span className={`font-medium ${color}`}>{status.replaceAll('_', ' ')}</span>
}

const columns = [
  helper.accessor('summary', {
    header: 'Summary',
    cell: (ctx) => (
      <Link
        to="/portfolio/theses/$thesisId"
        params={{ thesisId: ctx.row.original.thesis_id }}
        className="text-primary line-clamp-2 underline-offset-2 hover:underline"
      >
        {ctx.getValue()}
      </Link>
    ),
  }),
  helper.accessor('thesis_status', {
    header: 'Classification',
    cell: (ctx) => {
      const v = ctx.getValue()
      return v ? <StatusBadge status={v} /> : <span className="text-muted-foreground">—</span>
    },
  }),
  helper.accessor('status', {
    header: 'Status',
  }),
  helper.accessor('position_id', {
    header: 'Position',
    cell: (ctx) => (
      <Link
        to="/portfolio/positions/$positionId"
        params={{ positionId: ctx.getValue() }}
        className="text-primary font-mono text-xs underline-offset-2 hover:underline"
      >
        {ctx.getValue()}
      </Link>
    ),
  }),
  helper.accessor('age_hours', {
    header: 'Age',
    cell: (ctx) => fmtAge(ctx.getValue()),
  }),
  helper.accessor('position_unrealized_pl_usd', {
    header: 'Unrealized P/L',
    cell: (ctx) => fmtUsd(ctx.getValue()),
  }),
  helper.accessor('resolution_category', {
    header: 'Resolution',
    cell: (ctx) => ctx.getValue()?.replaceAll('_', ' ') ?? '—',
  }),
]

export function ThesesTable({ theses, total }: Props): React.JSX.Element {
  const table = useReactTable({
    data: theses,
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Theses ({total})</CardTitle>
      </CardHeader>
      <CardContent>
        {theses.length === 0 ? (
          <p className="text-muted-foreground text-sm">No theses match the current filters.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                {table.getHeaderGroups().map((hg) => (
                  <tr key={hg.id} className="text-muted-foreground border-b">
                    {hg.headers.map((h) => (
                      <th key={h.id} className="py-2 pr-4 text-left font-normal">
                        {flexRender(h.column.columnDef.header, h.getContext())}
                      </th>
                    ))}
                  </tr>
                ))}
              </thead>
              <tbody>
                {table.getRowModel().rows.map((row) => (
                  <tr key={row.id} className="border-b last:border-0">
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="py-2 pr-4">
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
