// Positions table (ALP-676).
// One row per open position; rows deep-link to /portfolio/positions/$positionId
// (story 05g).  Built with TanStack Table (headless).

import { Link } from '@tanstack/react-router'
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table'

import type { PositionRow } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { positions: PositionRow[] }

const helper = createColumnHelper<PositionRow>()

function fmtUsd(usdStr: string): string {
  const num = Number(usdStr)
  if (!Number.isFinite(num)) {
    return usdStr
  }
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(num)
}

function fmtAge(hours: number): string {
  if (hours < 24) {
    return `${hours.toFixed(1)}h`
  }
  return `${(hours / 24).toFixed(1)}d`
}

function fmtPct(val: number | null): string {
  if (val === null) {
    return '—'
  }
  return `${val.toFixed(1)}%`
}

const columns = [
  helper.accessor('ticker', {
    header: 'Ticker',
    cell: (ctx) => (
      <Link
        to="/portfolio/positions/$positionId"
        params={{ positionId: ctx.row.original.position_id }}
        className="text-primary underline-offset-2 hover:underline"
      >
        {ctx.getValue()}
      </Link>
    ),
  }),
  helper.accessor('instrument_type', { header: 'Type' }),
  helper.accessor('direction', {
    header: 'Dir',
    cell: (ctx) => ctx.getValue() ?? '—',
  }),
  helper.accessor('quantity', {
    header: 'Qty',
    cell: (ctx) => ctx.getValue().toLocaleString(),
  }),
  helper.accessor('market_value_usd', {
    header: 'Market value',
    cell: (ctx) => fmtUsd(ctx.getValue()),
  }),
  helper.accessor('unrealized_pl_usd', {
    header: 'Unrealized P/L',
    cell: (ctx) => fmtUsd(ctx.getValue()),
  }),
  helper.accessor('thesis_status', {
    header: 'Thesis',
    cell: (ctx) => ctx.getValue() ?? '—',
  }),
  helper.accessor('age_hours', {
    header: 'Age',
    cell: (ctx) => fmtAge(ctx.getValue()),
  }),
  helper.accessor('distance_to_target_pct', {
    header: 'To target',
    cell: (ctx) => fmtPct(ctx.getValue()),
  }),
  helper.accessor('distance_to_nearest_invalidation_pct', {
    header: 'To inval.',
    cell: (ctx) => fmtPct(ctx.getValue()),
  }),
]

export function PositionsTable({ positions }: Props): React.JSX.Element {
  const table = useReactTable({
    data: positions,
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Positions ({positions.length})</CardTitle>
      </CardHeader>
      <CardContent>
        {positions.length === 0 ? (
          <p className="text-muted-foreground text-sm">No open positions.</p>
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
                      <td key={cell.id} className="py-2 pr-4 tabular-nums">
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
