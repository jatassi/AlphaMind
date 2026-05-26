// Recent breaches table (ALP-680).
// Shows activity_log entries for guardrail/risk events in the last 24 h.

import type { RecentBreachItem } from '@/api/risk'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { breaches: RecentBreachItem[] }

const BREACH_LABELS: Record<string, string> = {
  GUARDRAIL_REJECTION: 'Rejected',
  RISK_LIMIT_APPROACHED: 'Limit approached',
  RISK_PARAMETER_CHANGED: 'Parameter changed',
  HALT_ACTIVATED: 'Halt activated',
  HALT_LIFTED: 'Halt lifted',
}

function fmtTs(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  } catch {
    return iso
  }
}

export function RecentBreachesTable({ breaches }: Props): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Recent breach events (last 24 h)</CardTitle>
      </CardHeader>
      <CardContent>
        {breaches.length === 0 ? (
          <p className="text-muted-foreground text-sm">No breach events in the last 24 hours.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-muted-foreground">
                <th className="pb-2 text-left font-normal">Time</th>
                <th className="pb-2 text-left font-normal">Event</th>
                <th className="pb-2 text-left font-normal">Position</th>
                <th className="pb-2 text-left font-normal">Detail</th>
              </tr>
            </thead>
            <tbody>
              {breaches.map((b) => (
                <tr key={b.entry_id} className="border-border/40 border-t">
                  <td className="text-muted-foreground py-1 tabular-nums">{fmtTs(b.entry_at)}</td>
                  <td className="py-1 font-medium">
                    {BREACH_LABELS[b.event_type] ?? b.event_type}
                  </td>
                  <td className="text-muted-foreground py-1">{b.position_id ?? '—'}</td>
                  <td className="text-muted-foreground py-1 font-mono text-xs">
                    {b.detail_json.slice(0, 80)}
                    {b.detail_json.length > 80 ? '…' : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  )
}
