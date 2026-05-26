// Per-rule drill-down side panel (ALP-680).
// Renders breach response classification from breach-behavior.md.

import type { RuleStatus } from '@/api/risk'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = {
  rule: RuleStatus
  onClose: () => void
}

// Breach response classification per breach-behavior.md § Per-rule breach
// response classification.
const BREACH_RESPONSE: Record<string, { response: string; rationale: string }> = {
  daily_drawdown: {
    response: 'Immediate: halt mode',
    rationale: 'Drawdown compounds in real time; waiting for the next invocation is unacceptable.',
  },
  cumulative_drawdown: {
    response: 'Immediate: progressive reduction',
    rationale: 'Survival constraint. Engine begins reducing risk immediately.',
  },
  position_max_loss: {
    response: 'Immediate: close position',
    rationale: 'Risk budget exhausted and thesis is almost certainly wrong.',
  },
  sector_concentration: {
    response: 'Deferred to strategist → PM',
    rationale: 'Market-movement breaches mean positions are winning — not immediately dangerous.',
  },
  net_long_exposure: {
    response: 'Deferred to strategist → PM',
    rationale: 'Directional exposure growing from profitable positions is not an emergency.',
  },
  net_short_exposure: {
    response: 'Immediate if >110% / Deferred if ≤110%',
    rationale: 'Short squeezes accelerate losses. Small overage deferred; large overage immediate.',
  },
  gross_exposure: {
    response: 'Deferred to strategist → PM',
    rationale: 'Same as net exposure — market-movement breach is not an emergency.',
  },
}

function getBreachResponse(ruleName: string): { response: string; rationale: string } | null {
  // Exact match first, then prefix match for sector_ rules
  if (Object.hasOwn(BREACH_RESPONSE, ruleName)) {
    return BREACH_RESPONSE[ruleName]
  }
  if (ruleName.startsWith('sector_')) {
    return BREACH_RESPONSE.sector_concentration
  }
  return null
}

export function RuleDrillDownPanel({ rule, onClose }: Props): React.JSX.Element {
  const breachInfo = getBreachResponse(rule.rule_name)

  return (
    <Card className="border-l-primary border-l-4">
      <CardHeader className="flex flex-row items-center justify-between pb-2">
        <CardTitle className="font-mono text-sm">{rule.rule_name}</CardTitle>
        <button
          className="text-muted-foreground hover:text-foreground"
          onClick={onClose}
          type="button"
          aria-label="Close drill-down"
        >
          ✕
        </button>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
          <dt className="text-muted-foreground">Current value</dt>
          <dd className="tabular-nums">{rule.current_value.toFixed(4)}</dd>
          <dt className="text-muted-foreground">Limit</dt>
          <dd className="tabular-nums">{rule.limit_value.toFixed(4)}</dd>
          <dt className="text-muted-foreground">Headroom</dt>
          <dd className="tabular-nums">{rule.headroom_pct.toFixed(2)}%</dd>
          <dt className="text-muted-foreground">Zone</dt>
          <dd className="font-medium">{rule.zone}</dd>
        </dl>

        {breachInfo === null ? null : (
          <div className="bg-muted/50 space-y-1 rounded-md p-3">
            <p className="font-medium">Breach response (breach-behavior.md)</p>
            <p className="text-muted-foreground">
              <span className="text-foreground font-medium">{breachInfo.response}</span>
              {' — '}
              {breachInfo.rationale}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
