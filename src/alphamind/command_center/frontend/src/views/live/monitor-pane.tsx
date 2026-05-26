import { useState } from 'react'

import { type EventStreamMessage, useEventStream } from '@/api/events'
import { type MonitorStatus, useLiveView } from '@/api/queries'

// Continuous monitor pane (story 05b / ALP-672).
//
// Renders WebSocket connection state, time-since-connect, last fill, and
// breach-detector state. Live updates from monitor:* SSE events; falls back
// to useLiveView() one-shot snapshot on connection drop.

function nullMonitor(): MonitorStatus {
  return {
    websocket_connected: false,
    time_since_connect_seconds: null,
    last_fill_at: null,
    breach_active: false,
    breach_rule: null,
  }
}

function ConnectionBadge({ connected }: { connected: boolean }): React.JSX.Element {
  const cls = connected ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {connected ? 'Connected' : 'Disconnected'}
    </span>
  )
}

function formatSeconds(seconds: number | null): string {
  if (seconds === null) {
    return '—'
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`
  }
  const mins = Math.floor(seconds / 60)
  const secs = Math.round(seconds % 60)
  return `${String(mins)}m ${String(secs)}s`
}

function useMonitorEventState(): [
  MonitorStatus | null,
  Record<string, (m: EventStreamMessage) => void>,
] {
  const [state, setState] = useState<MonitorStatus | null>(null)
  const handlers = {
    'monitor:websocket_connected': (_msg: EventStreamMessage) => {
      setState((prev) => ({
        ...(prev ?? nullMonitor()),
        websocket_connected: true,
        time_since_connect_seconds: 0,
      }))
    },
    'monitor:websocket_disconnected': (_msg: EventStreamMessage) => {
      setState((prev) => ({
        ...(prev ?? nullMonitor()),
        websocket_connected: false,
        time_since_connect_seconds: null,
      }))
    },
    'monitor:fill_received': (_msg: EventStreamMessage) => {
      setState((prev) => ({ ...(prev ?? nullMonitor()), last_fill_at: new Date().toISOString() }))
    },
    'monitor:breach_detected': (msg: EventStreamMessage) => {
      const payload = msg.payload as Record<string, unknown>
      setState((prev) => ({
        ...(prev ?? nullMonitor()),
        breach_active: true,
        breach_rule: (payload.rule as string | null) ?? null,
      }))
    },
  }
  return [state, handlers]
}

type MonitorDlProps = { monitor: MonitorStatus }

function MonitorDl({ monitor }: MonitorDlProps): React.JSX.Element {
  return (
    <dl className="space-y-1 text-sm">
      <div className="flex items-center gap-2">
        <dt className="font-medium">WebSocket</dt>
        <dd>
          <ConnectionBadge connected={monitor.websocket_connected} />
        </dd>
      </div>
      {monitor.websocket_connected ? (
        <div className="flex gap-2">
          <dt className="font-medium">Connected for</dt>
          <dd>{formatSeconds(monitor.time_since_connect_seconds)}</dd>
        </div>
      ) : null}
      <div className="flex gap-2">
        <dt className="font-medium">Last fill</dt>
        <dd>{monitor.last_fill_at ?? '—'}</dd>
      </div>
      <div className="flex gap-2">
        <dt className="font-medium">Breach</dt>
        <dd>
          {monitor.breach_active ? (
            <span className="text-destructive font-medium">
              Active{monitor.breach_rule === null ? '' : `: ${monitor.breach_rule}`}
            </span>
          ) : (
            <span className="text-muted-foreground">None</span>
          )}
        </dd>
      </div>
    </dl>
  )
}

export function MonitorPane(): React.JSX.Element {
  const liveQuery = useLiveView()
  const [monitorState, handlers] = useMonitorEventState()

  useEventStream({ subscribers: handlers })

  const monitor = monitorState ?? liveQuery.data?.monitor ?? nullMonitor()

  return (
    <div className="rounded-lg border p-4">
      <h2 className="mb-3 text-base font-semibold">Continuous Monitor</h2>
      {liveQuery.isError ? (
        <p className="text-destructive mb-2 text-sm">Failed to load monitor status.</p>
      ) : null}
      <MonitorDl monitor={monitor} />
    </div>
  )
}
