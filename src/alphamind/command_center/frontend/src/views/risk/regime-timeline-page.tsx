// Regime timeline page — /risk/timeline (ALP-680).
//
// Recharts time-series with breach event markers + chronological event list.

import { useMemo, useState } from 'react'

import type { TooltipProps } from 'recharts'
import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useRegimeTimeline } from '@/api/risk'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

const EVENT_TYPE_LABELS: Record<string, string> = {
  GUARDRAIL_REJECTION: 'Guardrail rejection',
  RISK_LIMIT_APPROACHED: 'Limit approached',
  RISK_PARAMETER_CHANGED: 'Parameter changed',
  HALT_ACTIVATED: 'Halt activated',
  HALT_LIFTED: 'Halt lifted',
  EMERGENCY_INVOCATION_REQUESTED: 'Emergency invocation',
  PROFILE_SWITCHED: 'Profile switched',
}

const EVENT_COLORS: Record<string, string> = {
  GUARDRAIL_REJECTION: '#dc2626',
  RISK_LIMIT_APPROACHED: '#ea580c',
  RISK_PARAMETER_CHANGED: '#7c3aed',
  HALT_ACTIVATED: '#dc2626',
  HALT_LIFTED: '#16a34a',
  EMERGENCY_INVOCATION_REQUESTED: '#b45309',
  PROFILE_SWITCHED: '#2563eb',
}

function fmtTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString('en-US', {
      month: 'short',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function buildDefaultWindow(): { from: string; to: string } {
  const to = new Date()
  const from = new Date(to.getTime() - 24 * 60 * 60 * 1000)
  return { from: from.toISOString(), to: to.toISOString() }
}

// Convert ISO-8601 (``2026-05-25T12:34:56.000Z``) → the native
// ``datetime-local`` input format (``2026-05-25T12:34``). Drops sub-
// minute precision so the input control stays single-step; the API
// accepts the trimmed timestamp without ambiguity.
function isoToLocalInput(iso: string): string {
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) {
      return ''
    }
    const pad = (n: number): string => n.toString().padStart(2, '0')
    return `${d.getFullYear().toString()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
  } catch {
    return ''
  }
}

// Convert the native ``datetime-local`` value back to an ISO-8601
// timestamp the timeline API accepts. Empty input collapses to ``''``
// which the caller filters out before sending.
function localInputToIso(local: string): string {
  if (local === '') {
    return ''
  }
  const d = new Date(local)
  if (Number.isNaN(d.getTime())) {
    return ''
  }
  return d.toISOString()
}

type ScatterPoint = { x: number; event_type: string }

function ScatterTooltipContent(props: TooltipProps<number, string>): React.JSX.Element | null {
  if (!props.active || !props.payload?.length) {
    return null
  }
  const pt = props.payload[0].payload as ScatterPoint
  return (
    <div className="bg-background rounded border p-2 text-xs shadow">
      <p className="font-medium">{EVENT_TYPE_LABELS[pt.event_type] ?? pt.event_type}</p>
      <p className="text-muted-foreground">{fmtTs(new Date(pt.x).toISOString())}</p>
    </div>
  )
}

type TimelineEvent = {
  entry_at: string
  event_type: string
  event_group: string
  position_id: string | null
}

function EventScatterChart({ events }: { events: TimelineEvent[] }): React.JSX.Element {
  const chartData = events.map((e, i) => ({
    x: new Date(e.entry_at).getTime(),
    y: 1,
    event_type: e.event_type,
    index: i,
  }))

  if (chartData.length === 0) {
    return <p className="text-muted-foreground text-sm">No events in this window.</p>
  }

  return (
    <ResponsiveContainer width="100%" height={120}>
      <ScatterChart>
        <CartesianGrid strokeDasharray="3 3" />
        <XAxis
          dataKey="x"
          type="number"
          domain={['auto', 'auto']}
          tickFormatter={(v: number) =>
            new Date(v).toLocaleTimeString('en-US', {
              hour: '2-digit',
              minute: '2-digit',
            })
          }
          tick={{ fontSize: 11 }}
        />
        <YAxis hide />
        <Tooltip content={ScatterTooltipContent} />
        <Scatter data={chartData} dataKey="y" fill="#2563eb" />
      </ScatterChart>
    </ResponsiveContainer>
  )
}

function EventLogTable({ events }: { events: TimelineEvent[] }): React.JSX.Element {
  if (events.length === 0) {
    return <p className="text-muted-foreground text-sm">No events in this window.</p>
  }

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-muted-foreground">
          <th className="pb-2 text-left font-normal">Time</th>
          <th className="pb-2 text-left font-normal">Event</th>
          <th className="pb-2 text-left font-normal">Group</th>
          <th className="pb-2 text-left font-normal">Position</th>
        </tr>
      </thead>
      <tbody>
        {events.map((e, i) => (
          <tr
            key={`${e.entry_at}-${String(i)}`}
            className="border-border/40 border-t"
            style={{ color: EVENT_COLORS[e.event_type] }}
          >
            <td className="text-muted-foreground py-1 tabular-nums">{fmtTs(e.entry_at)}</td>
            <td className="py-1 font-medium">{EVENT_TYPE_LABELS[e.event_type] ?? e.event_type}</td>
            <td className="text-muted-foreground py-1 text-xs">{e.event_group}</td>
            <td className="text-muted-foreground py-1">{e.position_id ?? '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

type WindowControlsProps = {
  from: string
  to: string
  onChangeFrom: (iso: string) => void
  onChangeTo: (iso: string) => void
}

function WindowControls({
  from,
  to,
  onChangeFrom,
  onChangeTo,
}: WindowControlsProps): React.JSX.Element {
  return (
    <div className="flex flex-wrap items-end gap-3 text-sm">
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">From</span>
        <input
          type="datetime-local"
          value={isoToLocalInput(from)}
          onChange={(e) => {
            const iso = localInputToIso(e.target.value)
            if (iso !== '') {
              onChangeFrom(iso)
            }
          }}
          className="border-border bg-background h-9 rounded-md border px-2"
          aria-label="Window start"
        />
      </label>
      <label className="flex flex-col gap-1">
        <span className="text-muted-foreground">To</span>
        <input
          type="datetime-local"
          value={isoToLocalInput(to)}
          onChange={(e) => {
            const iso = localInputToIso(e.target.value)
            if (iso !== '') {
              onChangeTo(iso)
            }
          }}
          className="border-border bg-background h-9 rounded-md border px-2"
          aria-label="Window end"
        />
      </label>
    </div>
  )
}

type TimelineContentProps = {
  data: { from_ts: string; to_ts: string; events: TimelineEvent[] }
}

function TimelineContent({ data }: TimelineContentProps): React.JSX.Element {
  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Events</CardTitle>
          <p className="text-muted-foreground text-sm">
            {fmtTs(data.from_ts)} — {fmtTs(data.to_ts)}
          </p>
        </CardHeader>
        <CardContent>
          <EventScatterChart events={data.events} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Event log</CardTitle>
        </CardHeader>
        <CardContent>
          <EventLogTable events={data.events} />
        </CardContent>
      </Card>
    </>
  )
}

type TimelineBodyProps = {
  data: { from_ts: string; to_ts: string; events: TimelineEvent[] } | undefined
  isPending: boolean
  isError: boolean
}

function TimelineBody({ data, isPending, isError }: TimelineBodyProps): React.JSX.Element {
  if (isPending) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-muted-foreground">Loading regime timeline…</span>
      </div>
    )
  }
  if (isError || data === undefined) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-destructive">Failed to load regime timeline.</span>
      </div>
    )
  }
  return <TimelineContent data={data} />
}

export function RegimeTimelinePage(): React.JSX.Element {
  const defaultWindow = useMemo(() => buildDefaultWindow(), [])
  const [from, setFrom] = useState(defaultWindow.from)
  const [to, setTo] = useState(defaultWindow.to)

  const { data, isPending, isError } = useRegimeTimeline(from, to)

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Regime &amp; overlay timeline</h1>
      <WindowControls from={from} to={to} onChangeFrom={setFrom} onChangeTo={setTo} />
      <TimelineBody data={data} isPending={isPending} isError={isError} />
    </div>
  )
}
