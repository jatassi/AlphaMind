import { useQueryClient } from '@tanstack/react-query'

import { useEventStream } from '@/api/events'
import { type ScheduleTrigger, scheduleViewQueryOptions, useScheduleView } from '@/api/queries'

// Schedule preview panel (story 05b / ALP-672).
//
// Renders the next (up to 5) APScheduler trigger fires + pause state.
// Sourced from GET /api/views/schedule (event-driven cache on the backend).
// Refreshes on pipeline:next_trigger_changed SSE events.

function TriggerRow({
  trigger,
  index,
}: {
  trigger: ScheduleTrigger
  index: number
}): React.JSX.Element {
  return (
    <li className="flex items-center gap-3 text-sm">
      <span className="text-muted-foreground w-6 text-right">{index + 1}.</span>
      <span className="font-mono text-xs">{trigger.trigger_at ?? '—'}</span>
      {trigger.trigger_type === null ? null : (
        <span className="bg-muted rounded px-1.5 py-0.5 text-xs capitalize">
          {trigger.trigger_type}
        </span>
      )}
    </li>
  )
}

function PausedBadge(): React.JSX.Element {
  return (
    <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
      Paused
    </span>
  )
}

function TriggerList({ triggers }: { triggers: ScheduleTrigger[] }): React.JSX.Element {
  return (
    <ol className="space-y-1.5">
      {triggers.map((t, i) => (
        <TriggerRow key={t.trigger_at ?? String(i)} trigger={t} index={i} />
      ))}
    </ol>
  )
}

function ScheduleBody({
  hasTriggers,
  isEmpty,
  isLoading,
  triggers,
}: {
  hasTriggers: boolean
  isEmpty: boolean
  isLoading: boolean
  triggers: ScheduleTrigger[]
}): React.JSX.Element {
  if (isLoading) {
    return <p className="text-muted-foreground text-sm">Loading…</p>
  }
  if (isEmpty) {
    return (
      <p className="text-muted-foreground text-sm">
        No upcoming triggers. Schedule data arrives with the first pipeline event.
      </p>
    )
  }
  if (hasTriggers) {
    return <TriggerList triggers={triggers} />
  }
  return <span />
}

export function SchedulePreview(): React.JSX.Element {
  const scheduleQuery = useScheduleView()
  const queryClient = useQueryClient()

  useEventStream({
    subscribers: {
      'pipeline:next_trigger_changed': () => {
        void queryClient.invalidateQueries({ queryKey: scheduleViewQueryOptions().queryKey })
      },
    },
  })

  const data = scheduleQuery.data
  const triggers = data?.triggers ?? []
  const hasTriggers = triggers.length > 0
  const isEmpty = !scheduleQuery.isLoading && triggers.length === 0

  return (
    <div className="rounded-lg border p-4">
      <div className="mb-3 flex items-center gap-3">
        <h2 className="text-base font-semibold">Schedule Preview</h2>
        {data?.paused === true ? <PausedBadge /> : null}
      </div>

      {scheduleQuery.isError ? (
        <p className="text-destructive text-sm">Failed to load schedule data.</p>
      ) : null}

      <ScheduleBody
        hasTriggers={hasTriggers}
        isEmpty={isEmpty}
        isLoading={scheduleQuery.isLoading}
        triggers={triggers}
      />

      {data?.cached_at !== undefined && data.cached_at !== null ? (
        <p className="text-muted-foreground mt-2 text-xs">Updated: {data.cached_at}</p>
      ) : null}
    </div>
  )
}
