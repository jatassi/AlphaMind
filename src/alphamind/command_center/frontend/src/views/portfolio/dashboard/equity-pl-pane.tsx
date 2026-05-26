// Equity and P/L pane (ALP-676).
// Displays: total value, high-water mark, current drawdown, daily/cumulative
// realized P/L, total unrealized P/L.

import type { EquityAndPL } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { data: EquityAndPL }

function fmt(usdStr: string): string {
  const num = Number(usdStr)
  if (!Number.isFinite(num)) {
    return usdStr
  }
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(num)
}

function fmtPct(pct: number): string {
  return `${pct.toFixed(2)}%`
}

export function EquityPlPane({ data }: Props): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Equity &amp; P/L</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted-foreground">Total value</dt>
          <dd className="font-medium tabular-nums">{fmt(data.total_value_usd)}</dd>

          <dt className="text-muted-foreground">High-water mark</dt>
          <dd className="font-medium tabular-nums">{fmt(data.high_water_mark_usd)}</dd>

          <dt className="text-muted-foreground">Current drawdown</dt>
          <dd className="text-destructive font-medium tabular-nums">
            {fmtPct(data.current_drawdown_pct)}
          </dd>

          <dt className="text-muted-foreground">Daily realized P/L</dt>
          <dd className="font-medium tabular-nums">{fmt(data.daily_realized_pl_usd)}</dd>

          <dt className="text-muted-foreground">Cumulative realized P/L</dt>
          <dd className="font-medium tabular-nums">{fmt(data.cumulative_realized_pl_usd)}</dd>

          <dt className="text-muted-foreground">Total unrealized P/L</dt>
          <dd className="font-medium tabular-nums">{fmt(data.total_unrealized_pl_usd)}</dd>
        </dl>
      </CardContent>
    </Card>
  )
}
