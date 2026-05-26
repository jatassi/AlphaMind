// Cash and capital pane (ALP-676).
// Displays: cash, settled cash, reserved capital, buying power, margin held,
// unsettled proceeds, Reg T excess trailing windows.

import type { CashAndCapital } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { data: CashAndCapital }

function fmt(usdStr: string): string {
  const num = Number(usdStr)
  if (!Number.isFinite(num)) {
    return usdStr
  }
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(num)
}

export function CashCapitalPane({ data }: Props): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Cash &amp; Capital</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted-foreground">Cash</dt>
          <dd className="font-medium tabular-nums">{fmt(data.cash_usd)}</dd>

          <dt className="text-muted-foreground">Settled cash</dt>
          <dd className="font-medium tabular-nums">{fmt(data.settled_cash_usd)}</dd>

          <dt className="text-muted-foreground">Reserved capital</dt>
          <dd className="font-medium tabular-nums">{fmt(data.reserved_capital_usd)}</dd>

          <dt className="text-muted-foreground">Available buying power</dt>
          <dd className="font-medium tabular-nums">{fmt(data.available_buying_power_usd)}</dd>

          <dt className="text-muted-foreground">Margin held</dt>
          <dd className="font-medium tabular-nums">{fmt(data.margin_held_usd)}</dd>

          {data.unsettled_proceeds.length > 0 && (
            <>
              <dt className="col-span-2 mt-2 font-medium">Unsettled proceeds</dt>
              {data.unsettled_proceeds.map((item) => (
                <div key={item.source_transaction_id} className="col-span-2 grid grid-cols-2">
                  <dt className="text-muted-foreground">{item.settlement_date}</dt>
                  <dd className="font-medium tabular-nums">{fmt(item.amount_usd)}</dd>
                </div>
              ))}
            </>
          )}

          <dt className="col-span-2 mt-2 font-medium">Reg T excess vs portfolio margin</dt>

          <dt className="text-muted-foreground">Trailing 30d</dt>
          <dd className="font-medium tabular-nums">{fmt(data.regt_excess.trailing_30d_usd)}</dd>

          <dt className="text-muted-foreground">Trailing 90d</dt>
          <dd className="font-medium tabular-nums">{fmt(data.regt_excess.trailing_90d_usd)}</dd>

          <dt className="text-muted-foreground">Lifetime</dt>
          <dd className="font-medium tabular-nums">{fmt(data.regt_excess.lifetime_usd)}</dd>
        </dl>
      </CardContent>
    </Card>
  )
}
