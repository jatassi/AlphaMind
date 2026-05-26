// Exposure pane (ALP-676).
// Gross/net/sector breakdown + delta-adjusted net.

import type { Exposure } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { data: Exposure }

function fmtPct(n: number): string {
  return `${n.toFixed(1)}%`
}

function fmtUsd(usdStr: string): string {
  const num = Number(usdStr)
  if (!Number.isFinite(num)) {
    return usdStr
  }
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(num)
}

type SectorBreakdownItem = Exposure['sector_breakdown'][number]

function SectorTable({ rows }: { rows: SectorBreakdownItem[] }): React.JSX.Element {
  return (
    <div>
      <p className="mb-2 text-sm font-medium">Sector breakdown</p>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-muted-foreground">
            <th className="pb-1 text-left font-normal">Sector</th>
            <th className="pb-1 text-right font-normal">Long</th>
            <th className="pb-1 text-right font-normal">Short</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => (
            <tr key={item.sector}>
              <td className="py-0.5">{item.sector}</td>
              <td className="py-0.5 text-right tabular-nums">
                {fmtUsd(item.long_market_value_usd)}
              </td>
              <td className="py-0.5 text-right tabular-nums">
                {fmtUsd(item.short_market_value_usd)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function ExposurePane({ data }: Props): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Exposure</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid gap-6 md:grid-cols-2">
          <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
            <dt className="text-muted-foreground">Gross exposure</dt>
            <dd className="font-medium tabular-nums">{fmtPct(data.gross_exposure_pct)}</dd>
            <dt className="text-muted-foreground">Net long</dt>
            <dd className="font-medium tabular-nums">{fmtPct(data.net_long_pct)}</dd>
            <dt className="text-muted-foreground">Net short</dt>
            <dd className="font-medium tabular-nums">{fmtPct(data.net_short_pct)}</dd>
            <dt className="text-muted-foreground">Delta-adj. net</dt>
            <dd className="font-medium tabular-nums">{fmtUsd(data.delta_adjusted_net_usd)}</dd>
          </dl>
          {data.sector_breakdown.length > 0 && <SectorTable rows={data.sector_breakdown} />}
        </div>
      </CardContent>
    </Card>
  )
}
