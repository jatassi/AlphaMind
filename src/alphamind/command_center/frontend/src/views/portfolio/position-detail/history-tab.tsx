// Position detail — History tab (ALP-677).
// Embeds the activity-log explorer pre-filtered by position_id.
// The filter is injected as a fixed prop; the user cannot clear it
// (the full explorer is available from the nav for unfiltered access).

import type { ActivityLogEntry } from '@/api/portfolio'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

type Props = { positionId: string; entries: ActivityLogEntry[] }

function fmtTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function summarize(detailJson: string): string {
  try {
    const parsed = JSON.parse(detailJson) as unknown
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      const d = parsed as Record<string, unknown>
      const candidate = d.description ?? d.reason ?? d.verdict ?? null
      if (typeof candidate === 'string' && candidate) {
        return candidate.slice(0, 100)
      }
    }
  } catch {
    // fall through
  }
  return ''
}

export function HistoryTab({ positionId, entries }: Props): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Activity log for position <code className="text-sm">{positionId}</code>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {entries.length === 0 ? (
          <p className="text-muted-foreground text-sm">No activity log entries.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground border-b">
                  <th className="py-2 pr-4 text-left font-normal">Time</th>
                  <th className="py-2 pr-4 text-left font-normal">Event</th>
                  <th className="py-2 pr-4 text-left font-normal">Source</th>
                  <th className="py-2 pr-4 text-left font-normal">Summary</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((entry) => {
                  const summary = summarize(entry.detail_json)
                  return (
                    <tr key={entry.entry_id} className="border-b last:border-0">
                      <td className="py-2 pr-4 text-xs tabular-nums">{fmtTs(entry.entry_at)}</td>
                      <td className="py-2 pr-4">{entry.event_type}</td>
                      <td className="py-2 pr-4">{entry.source}</td>
                      <td className="text-muted-foreground py-2 pr-4 text-xs">{summary || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
