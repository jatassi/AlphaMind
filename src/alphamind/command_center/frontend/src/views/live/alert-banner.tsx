import { useState } from 'react'

import { useQueryClient } from '@tanstack/react-query'

import { useEventStream } from '@/api/events'
import { type AlertSummary, liveViewQueryOptions, useLiveView } from '@/api/queries'

// Active-alerts banner (story 05b / ALP-672).
//
// Sticky top-of-page strip showing unacknowledged alerts grouped by severity.
// Refreshes within ~1s of a cc:alert_fired SSE event by invalidating the live
// view query. Clicking navigates to the alert detail stub route.

const SEVERITY_CLASS: Record<string, string> = {
  critical: 'bg-red-100 border-red-300 text-red-900',
  warning: 'bg-amber-50 border-amber-300 text-amber-900',
  info: 'bg-blue-50 border-blue-300 text-blue-900',
}

function severityClass(severity: string): string {
  return SEVERITY_CLASS[severity.toLowerCase()] ?? 'bg-muted border-border text-foreground'
}

function navigateToAlert(alertId: string): void {
  globalThis.location.assign(`/alerts/${alertId}`)
}

function AlertChip({ alert }: { alert: AlertSummary }): React.JSX.Element {
  return (
    <div
      role="button"
      tabIndex={0}
      className={`cursor-pointer rounded border px-3 py-1.5 text-sm ${severityClass(alert.severity)}`}
      title={`Alert ${alert.alert_id} — ${alert.rule_name}`}
      onClick={() => navigateToAlert(alert.alert_id)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          navigateToAlert(alert.alert_id)
        }
      }}
    >
      <span className="font-medium capitalize">{alert.severity}</span>
      {' — '}
      {alert.rule_name}
    </div>
  )
}

// AlertBannerLive is used by the live page; the top-level AlertBanner in the
// layout is the placeholder (for future global alert state via story 05a).
export function AlertBannerLive(): React.JSX.Element | null {
  const liveQuery = useLiveView()
  const queryClient = useQueryClient()
  const [flashCount, setFlashCount] = useState(0)

  useEventStream({
    subscribers: {
      'cc:alert_fired': () => {
        void queryClient.invalidateQueries({ queryKey: liveViewQueryOptions().queryKey })
        setFlashCount((n) => n + 1)
      },
    },
  })

  const alerts = liveQuery.data?.active_alerts ?? []
  if (alerts.length === 0) {
    return null
  }

  return (
    <div
      className="bg-muted/50 flex flex-wrap gap-2 rounded border p-2"
      aria-label={`${String(alerts.length)} active alert${alerts.length === 1 ? '' : 's'}`}
      data-flash-count={flashCount}
    >
      {alerts.map((alert) => (
        <AlertChip key={alert.alert_id} alert={alert} />
      ))}
    </div>
  )
}
