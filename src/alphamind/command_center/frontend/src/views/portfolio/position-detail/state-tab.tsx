// Position detail — State tab (ALP-677).
// Displays current position fields, bracket legs table, and fill history.

import type { BracketLegDetail, FillDetail, PositionDetail } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { data: PositionDetail }

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

function PositionFields({ data }: { data: PositionDetail }): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Position</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted-foreground">Ticker</dt>
          <dd className="font-medium">{data.ticker}</dd>

          <dt className="text-muted-foreground">Type</dt>
          <dd>{data.instrument_type}</dd>

          <dt className="text-muted-foreground">Direction</dt>
          <dd>{data.direction ?? '—'}</dd>

          <dt className="text-muted-foreground">Status</dt>
          <dd>{data.status}</dd>

          <dt className="text-muted-foreground">Quantity</dt>
          <dd className="tabular-nums">{data.quantity.toLocaleString()}</dd>

          <dt className="text-muted-foreground">Market value</dt>
          <dd className="tabular-nums">{fmtUsd(data.market_value_usd)}</dd>

          <dt className="text-muted-foreground">Unrealized P/L</dt>
          <dd className="tabular-nums">{fmtUsd(data.unrealized_pl_usd)}</dd>

          {data.realized_pl_usd !== null && (
            <>
              <dt className="text-muted-foreground">Realized P/L</dt>
              <dd className="tabular-nums">{fmtUsd(data.realized_pl_usd)}</dd>
            </>
          )}

          <dt className="text-muted-foreground">Age</dt>
          <dd className="tabular-nums">{fmtAge(data.age_hours)}</dd>

          <dt className="text-muted-foreground">To target</dt>
          <dd className="tabular-nums">{fmtPct(data.distance_to_target_pct)}</dd>

          <dt className="text-muted-foreground">To invalidation</dt>
          <dd className="tabular-nums">{fmtPct(data.distance_to_nearest_invalidation_pct)}</dd>
        </dl>
      </CardContent>
    </Card>
  )
}

function BracketLegsTable({ legs }: { legs: BracketLegDetail[] }): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Bracket legs ({legs.length})</CardTitle>
      </CardHeader>
      <CardContent>
        {legs.length === 0 ? (
          <p className="text-muted-foreground text-sm">No bracket legs.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground border-b">
                  <th className="py-2 pr-4 text-left font-normal">#</th>
                  <th className="py-2 pr-4 text-left font-normal">Type</th>
                  <th className="py-2 pr-4 text-left font-normal">Trigger</th>
                  <th className="py-2 pr-4 text-left font-normal">Enforcement</th>
                  <th className="py-2 pr-4 text-left font-normal">Status</th>
                </tr>
              </thead>
              <tbody>
                {legs.map((leg) => (
                  <tr key={leg.bracket_leg_id} className="border-b last:border-0">
                    <td className="py-2 pr-4 tabular-nums">{leg.leg_index}</td>
                    <td className="py-2 pr-4">{leg.leg_type}</td>
                    <td className="py-2 pr-4">{leg.trigger_kind}</td>
                    <td className="py-2 pr-4">{leg.enforcement}</td>
                    <td className="py-2 pr-4">{leg.leg_status}</td>
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

function FillHistoryTable({ fills }: { fills: FillDetail[] }): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Fill history ({fills.length})</CardTitle>
      </CardHeader>
      <CardContent>
        {fills.length === 0 ? (
          <p className="text-muted-foreground text-sm">No fills.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground border-b">
                  <th className="py-2 pr-4 text-left font-normal">Timestamp</th>
                  <th className="py-2 pr-4 text-left font-normal">Price</th>
                  <th className="py-2 pr-4 text-left font-normal">Qty</th>
                  <th className="py-2 pr-4 text-left font-normal">Fees</th>
                  <th className="py-2 pr-4 text-left font-normal">Status after</th>
                </tr>
              </thead>
              <tbody>
                {fills.map((fill) => (
                  <tr key={fill.fill_id} className="border-b last:border-0">
                    <td className="py-2 pr-4 tabular-nums">
                      {new Date(fill.fill_timestamp).toLocaleString()}
                    </td>
                    <td className="py-2 pr-4 tabular-nums">{fmtUsd(fill.fill_price)}</td>
                    <td className="py-2 pr-4 tabular-nums">
                      {fill.fill_quantity.toLocaleString()}
                    </td>
                    <td className="py-2 pr-4 tabular-nums">{fmtUsd(fill.fees_usd)}</td>
                    <td className="py-2 pr-4">{fill.order_status_after}</td>
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

export function StateTab({ data }: Props): React.JSX.Element {
  return (
    <div className="space-y-4">
      <PositionFields data={data} />
      <BracketLegsTable legs={data.bracket_legs} />
      <FillHistoryTable fills={data.fills} />
    </div>
  )
}
