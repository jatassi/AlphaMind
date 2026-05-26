// Per-rule guardrail status table (ALP-680).
// Renders a row per rule with zone colour-coding and headroom bar.

import type { RuleStatus } from '@/api/risk'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = {
  rules: RuleStatus[]
  onRuleClick?: (ruleName: string) => void
}

const ZONE_COLORS = {
  normal: 'text-green-600',
  warning: 'text-yellow-600',
  critical: 'text-orange-600',
  hard_block: 'text-red-600',
} as const

const ZONE_BAR_COLORS = {
  normal: 'bg-green-500',
  warning: 'bg-yellow-500',
  critical: 'bg-orange-500',
  hard_block: 'bg-red-600',
} as const

function HeadroomBar({ headroom, zone }: { headroom: number; zone: RuleStatus['zone'] }) {
  const fill = Math.max(0, Math.min(100, headroom))
  return (
    <div className="bg-muted h-1.5 w-24 rounded-full">
      <div
        className={`h-full rounded-full ${ZONE_BAR_COLORS[zone]}`}
        style={{ width: `${String(fill)}%` }}
      />
    </div>
  )
}

export function RuleStatusTable({ rules, onRuleClick }: Props): React.JSX.Element {
  if (rules.length === 0) {
    return <p className="text-muted-foreground text-sm">No rules available.</p>
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Guardrail rules</CardTitle>
      </CardHeader>
      <CardContent>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-muted-foreground">
              <th className="pb-2 text-left font-normal">Rule</th>
              <th className="pb-2 text-right font-normal">Current</th>
              <th className="pb-2 text-right font-normal">Limit</th>
              <th className="pb-2 text-right font-normal">Headroom</th>
              <th className="pb-2 pl-4 text-left font-normal">Zone</th>
              <th className="pb-2" />
            </tr>
          </thead>
          <tbody>
            {rules.map((rule) => (
              <tr
                key={rule.rule_name}
                className={onRuleClick ? 'hover:bg-muted/50 cursor-pointer' : ''}
                onClick={() => onRuleClick?.(rule.rule_name)}
              >
                <td className="py-1 font-mono text-xs">{rule.rule_name}</td>
                <td className="py-1 text-right tabular-nums">{rule.current_value.toFixed(2)}</td>
                <td className="text-muted-foreground py-1 text-right tabular-nums">
                  {rule.limit_value.toFixed(2)}
                </td>
                <td className="py-1 text-right tabular-nums">{rule.headroom_pct.toFixed(1)}%</td>
                <td className={`py-1 pl-4 font-medium ${ZONE_COLORS[rule.zone]}`}>{rule.zone}</td>
                <td className="py-1 pl-4">
                  <HeadroomBar headroom={rule.headroom_pct} zone={rule.zone} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  )
}
