import { useQueryClient } from '@tanstack/react-query'

import { useEventStream } from '@/api/events'
import { liveViewQueryOptions, scheduleViewQueryOptions } from '@/api/queries'

import { AlertBannerLive } from './alert-banner'
import { MonitorPane } from './monitor-pane'
import { PipelinePane } from './pipeline-pane'
import { SchedulePreview } from './schedule-preview'

// Live run watcher page (story 05b / ALP-672).
//
// Assembles three panes side-by-side (pipeline status, monitor state,
// active alerts banner) plus the schedule preview panel below.
//
// SSE subscriptions keep each pane fresh; on connection drop TanStack Query's
// staleTime / refetchOnWindowFocus combination serves as the one-shot
// fallback until SSE reconnects.

export function LivePage(): React.JSX.Element {
  const queryClient = useQueryClient()

  // Invalidate the live view on any pipeline or monitor event so TanStack
  // Query re-fetches the one-shot snapshot on the next render cycle. This
  // is the connection-drop fallback path: when SSE is connected the view
  // state comes from the events; when SSE drops the query re-fetches
  // automatically on the next stale boundary.
  useEventStream({
    subscribers: {
      'pipeline:*': () => {
        void queryClient.invalidateQueries({ queryKey: liveViewQueryOptions().queryKey })
      },
      'monitor:*': () => {
        void queryClient.invalidateQueries({ queryKey: liveViewQueryOptions().queryKey })
      },
      'pipeline:next_trigger_changed': () => {
        void queryClient.invalidateQueries({ queryKey: scheduleViewQueryOptions().queryKey })
      },
    },
  })

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Live Operations</h1>

      {/* Active alerts banner — sticky strip above the main panes. */}
      <AlertBannerLive />

      {/* Three-pane row: pipeline status / monitor / (future: guardrails). */}
      <div className="grid gap-4 lg:grid-cols-2">
        <PipelinePane />
        <MonitorPane />
      </div>

      {/* Schedule preview — next triggers + pause state. */}
      <SchedulePreview />
    </div>
  )
}
