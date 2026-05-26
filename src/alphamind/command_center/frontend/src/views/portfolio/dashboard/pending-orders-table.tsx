// Pending orders table (ALP-676).
// One row per PENDING / PARTIALLY_FILLED order.  Order rows are not
// clickable in v1 — no order detail view exists yet.

import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table'

import type { PendingOrderRow } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { orders: PendingOrderRow[] }

const helper = createColumnHelper<PendingOrderRow>()

function fmtAge(hours: number): string {
  if (hours < 24) {
    return `${hours.toFixed(1)}h`
  }
  return `${(hours / 24).toFixed(1)}d`
}

function tickerFromSpec(spec: Record<string, unknown>): string {
  const t = spec.ticker ?? spec.symbol ?? spec.underlying
  return typeof t === 'string' ? t : '—'
}

const columns = [
  helper.accessor('order_id', {
    header: 'Order ID',
    cell: (ctx) => <span className="font-mono text-xs">{ctx.getValue()}</span>,
  }),
  helper.accessor('instrument_spec', {
    header: 'Instrument',
    cell: (ctx) => tickerFromSpec(ctx.getValue()),
  }),
  helper.accessor('order_type', { header: 'Type' }),
  helper.accessor('order_role', { header: 'Role' }),
  helper.accessor('direction', {
    header: 'Dir',
    cell: (ctx) => ctx.getValue() ?? '—',
  }),
  helper.accessor('quantity', {
    header: 'Qty',
    cell: (ctx) => ctx.getValue().toLocaleString(),
  }),
  helper.accessor('filled_quantity', {
    header: 'Filled',
    cell: (ctx) => ctx.getValue().toLocaleString(),
  }),
  helper.accessor('remaining_quantity', {
    header: 'Remaining',
    cell: (ctx) => ctx.getValue().toLocaleString(),
  }),
  helper.accessor('status', { header: 'Status' }),
  helper.accessor('age_hours', {
    header: 'Age',
    cell: (ctx) => fmtAge(ctx.getValue()),
  }),
]

export function PendingOrdersTable({ orders }: Props): React.JSX.Element {
  const table = useReactTable({
    data: orders,
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Pending orders ({orders.length})</CardTitle>
      </CardHeader>
      <CardContent>
        {orders.length === 0 ? (
          <p className="text-muted-foreground text-sm">No pending orders.</p>
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
