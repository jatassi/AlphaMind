// Commands and fills pane for the per-invocation detail page (ALP-674).
//
// Renders ORDER_SUBMITTED, ORDER_FILLED, ORDER_REJECTED, GUARDRAIL_REJECTION,
// COMMAND_ABANDONED, and other command/fill activity-log entries.

import type { InvocationActivityEntry } from '@/api/queries'

const EVENT_CLS: Record<string, string> = {
  ORDER_SUBMITTED: 'bg-blue-50 text-blue-700',
  ORDER_FILLED: 'bg-green-50 text-green-700',
  ORDER_PARTIALLY_FILLED: 'bg-yellow-50 text-yellow-700',
  ORDER_CANCELLED: 'bg-muted text-muted-foreground',
  ORDER_EXPIRED: 'bg-muted text-muted-foreground',
  ORDER_REJECTED: 'bg-red-50 text-red-700',
  ORDER_MODIFIED: 'bg-yellow-50 text-yellow-700',
  COMMAND_ABANDONED: 'bg-red-50 text-red-700',
  GUARDRAIL_REJECTION: 'bg-red-50 text-red-700',
}

type EntryRowProps = { entry: InvocationActivityEntry }

function EntityId({ detail }: { detail: Record<string, unknown> }): React.JSX.Element {
  if (typeof detail.order_id === 'string') {
    return <span>{detail.order_id}</span>
  }
  if (typeof detail.command_id === 'string') {
    return <span>{detail.command_id}</span>
  }
  return <span>—</span>
}

function EntryRow({ entry }: EntryRowProps): React.JSX.Element {
  const cls = EVENT_CLS[entry.event_type] ?? 'bg-muted text-muted-foreground'
  let detail: Record<string, unknown> = {}
  try {
    detail = JSON.parse(entry.detail_json) as Record<string, unknown>
  } catch {
    // ignore
  }

  return (
    <tr className="border-b last:border-none">
      <td className="px-3 py-2">
        <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${cls}`}>
          {entry.event_type}
        </span>
      </td>
      <td className="text-muted-foreground px-3 py-2 text-xs">{entry.entry_at}</td>
      <td className="px-3 py-2 font-mono text-xs">
        <EntityId detail={detail} />
      </td>
      <td className="px-3 py-2 text-xs">{entry.source}</td>
    </tr>
  )
}

type CommandsPaneProps = { entries: InvocationActivityEntry[] }

export function CommandsPane({ entries }: CommandsPaneProps): React.JSX.Element {
  return (
    <section aria-label="Commands and fills">
      <h2 className="mb-2 text-lg font-semibold">Commands and fills</h2>
      {entries.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          No command or fill entries for this invocation.
        </p>
      ) : (
        <div className="overflow-x-auto rounded border">
          <table className="w-full text-left text-sm">
            <thead className="bg-muted/40 border-b">
              <tr>
                <th className="px-3 py-2 font-medium">Event</th>
                <th className="px-3 py-2 font-medium">Timestamp</th>
                <th className="px-3 py-2 font-medium">Order / Command ID</th>
                <th className="px-3 py-2 font-medium">Source</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <EntryRow key={e.entry_id} entry={e} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
