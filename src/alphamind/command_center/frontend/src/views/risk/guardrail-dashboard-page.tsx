// Guardrail dashboard page — /risk (ALP-680).
//
// Composes per-rule status table, drawdown pane, active overlays pane,
// recent breaches table. Clicking a rule row opens the drill-down side panel.

import { useState } from 'react'

import { useGuardrailDashboard } from '@/api/risk'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

import { DrawdownPane } from './drawdown-pane'
import { RecentBreachesTable } from './recent-breaches-table'
import { RuleDrillDownPanel } from './rule-drill-down-panel'
import { RuleStatusTable } from './rule-status-table'

type Multiplier = { rule_name: string; multiplier: number; effective_limit: number }

function MultipliersTable({ multipliers }: { multipliers: Multiplier[] }): React.JSX.Element {
  if (multipliers.length === 0) {
    return <p className="text-muted-foreground">No regime multipliers active.</p>
  }
  return (
    <table className="w-full">
      <thead>
        <tr className="text-muted-foreground">
          <th className="pb-1 text-left font-normal">Rule</th>
          <th className="pb-1 text-right font-normal">Multiplier</th>
          <th className="pb-1 text-right font-normal">Effective limit</th>
        </tr>
      </thead>
      <tbody>
        {multipliers.map((m) => (
          <tr key={m.rule_name}>
            <td className="py-0.5 font-mono text-xs">{m.rule_name}</td>
            <td className="py-0.5 text-right tabular-nums">{m.multiplier.toFixed(2)}×</td>
            <td className="py-0.5 text-right tabular-nums">{m.effective_limit.toFixed(2)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

type MultipliersPaneData = {
  regime_label: string | null
  multipliers: Multiplier[]
  active_overlays: string[]
  halt_mode_engaged: boolean
}

function MultipliersPane({ data }: { data: MultipliersPaneData }): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Active regime &amp; overlays
          {data.halt_mode_engaged ? (
            <span className="ml-2 rounded bg-red-100 px-1.5 py-0.5 text-xs font-semibold text-red-700">
              HALT
            </span>
          ) : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="text-sm">
        <p className="mb-3">
          <span className="text-muted-foreground">Regime: </span>
          <span className="font-medium">{data.regime_label ?? 'Unknown'}</span>
        </p>
        {data.active_overlays.length > 0 ? (
          <div className="mb-3">
            <p className="text-muted-foreground mb-1">Active overlays</p>
            <ul className="list-inside list-disc space-y-0.5">
              {data.active_overlays.map((o) => (
                <li key={o}>{o}</li>
              ))}
            </ul>
          </div>
        ) : null}
        <MultipliersTable multipliers={data.multipliers} />
      </CardContent>
    </Card>
  )
}

export function GuardrailDashboardPage(): React.JSX.Element {
  const { data, isPending, isError } = useGuardrailDashboard()
  const [selectedRule, setSelectedRule] = useState<string | null>(null)

  if (isPending) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-muted-foreground">Loading guardrail dashboard…</span>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-destructive">Failed to load guardrail data.</span>
      </div>
    )
  }

  const selectedRuleData = selectedRule
    ? (data.rules.find((r) => r.rule_name === selectedRule) ?? null)
    : null

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Risk &amp; Guardrails</h1>

      <div className="grid gap-4 md:grid-cols-2">
        <DrawdownPane data={data.drawdown} />
        <MultipliersPane data={data.active_multipliers_and_overlays} />
      </div>

      <div className={selectedRuleData === null ? '' : 'grid gap-4 lg:grid-cols-3'}>
        <div className={selectedRuleData === null ? '' : 'lg:col-span-2'}>
          <RuleStatusTable
            rules={data.rules}
            onRuleClick={(name) => setSelectedRule(name === selectedRule ? null : name)}
          />
        </div>
        {selectedRuleData === null ? null : (
          <RuleDrillDownPanel rule={selectedRuleData} onClose={() => setSelectedRule(null)} />
        )}
      </div>

      <RecentBreachesTable breaches={data.recent_breaches} />
    </div>
  )
}
