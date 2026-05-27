// Drawdown status pane (ALP-680).
// Displays daily + cumulative drawdown, progressive tier, halt mode flag.

import type { DrawdownStatus } from '@/api/risk'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { data: DrawdownStatus }

const ZONE_COLORS: Record<string, string> = {
  normal: 'text-green-600',
  warning: 'text-yellow-600',
  critical: 'text-orange-600',
  hard_block: 'text-red-600',
}

const TIER_LABELS: Record<number, string> = {
  0: 'Normal',
  1: 'Tier 1 (≥8%)',
  2: 'Tier 2 (≥10%)',
  3: 'Tier 3 — Full halt (≥12%)',
}

function fmtPct(n: number): string {
  return `${n.toFixed(2)}%`
}

export function DrawdownPane({ data }: Props): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Drawdown
          {data.halt_mode_engaged ? (
            <span className="ml-2 rounded bg-red-100 px-1.5 py-0.5 text-xs font-semibold text-red-700">
              HALT MODE
            </span>
          ) : null}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted-foreground">High-water mark</dt>
          <dd className="font-medium tabular-nums">{data.equity_high_water_mark_usd}</dd>

          <dt className="text-muted-foreground">Daily drawdown</dt>
          <dd className={`font-medium tabular-nums ${ZONE_COLORS[data.daily_zone] ?? ''}`}>
            {fmtPct(data.daily_drawdown_pct)}
            <span className="text-muted-foreground ml-1 text-xs">
              / {fmtPct(data.daily_drawdown_limit_pct)}
            </span>
          </dd>

          <dt className="text-muted-foreground">Daily zone</dt>
          <dd className={`font-medium ${ZONE_COLORS[data.daily_zone] ?? ''}`}>{data.daily_zone}</dd>

          <dt className="text-muted-foreground">Cumulative drawdown</dt>
          <dd className={`font-medium tabular-nums ${ZONE_COLORS[data.cumulative_zone] ?? ''}`}>
            {fmtPct(data.cumulative_drawdown_pct)}
            <span className="text-muted-foreground ml-1 text-xs">
              / {fmtPct(data.cumulative_drawdown_limit_pct)}
            </span>
          </dd>

          <dt className="text-muted-foreground">Cumulative zone</dt>
          <dd className={`font-medium ${ZONE_COLORS[data.cumulative_zone] ?? ''}`}>
            {data.cumulative_zone}
          </dd>

          <dt className="text-muted-foreground">Progressive tier</dt>
          <dd className={`font-medium ${data.progressive_tier > 0 ? 'text-orange-600' : ''}`}>
            {TIER_LABELS[data.progressive_tier] ?? `Tier ${String(data.progressive_tier)}`}
          </dd>
        </dl>
      </CardContent>
    </Card>
  )
}
