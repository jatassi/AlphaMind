import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { RunHistoryFilters, RunHistoryPage } from '@/views/history/types'

import { api, ApiError } from './client'

// TanStack Query hook factories. View stories add their own per-endpoint
// hooks here; the foundation ships `useSession()`, which the protected
// `_authed` route layout calls to gate access to the views.
//
// Query keys are arrays whose first element is the conceptual scope
// (`['session']`, `['runs']`, …) so per-view cache invalidation can target
// the right slice without leaking each view's URL shape into siblings.

export type SessionInfo = {
  session_id: string
  expires_at: string
}

const SESSION_QUERY_KEY = ['session'] as const

// Detail the /auth/me endpoint exposes — the resolved session id + expiry.
// The session row itself stays server-side; the frontend only needs to know
// "am I authenticated?" plus a freshness window for cache invalidation.
export function sessionQueryOptions(): ReturnType<typeof queryOptions<SessionInfo | null>> {
  return queryOptions<SessionInfo | null>({
    queryKey: SESSION_QUERY_KEY,
    queryFn: async () => {
      try {
        return await api.get<SessionInfo>('/auth/me')
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          return null
        }
        throw error
      }
    },
    // Retry once on transient network errors; refetch is cheap so we don't
    // need an aggressive stale time for v1. Per-view stories can override
    // their own queries' freshness if they need real-time semantics.
    retry: 1,
    staleTime: 30 * 1000,
  })
}

export function useSession(): UseQueryResult<SessionInfo | null> {
  return useQuery(sessionQueryOptions())
}

// ---------------------------------------------------------------------------
// Live view queries (story 05b / ALP-672)
// ---------------------------------------------------------------------------

export type InvocationStatus = {
  invocation_id: string | null
  run_type: string | null
  started_at: string | null
  ended_at: string | null
  status: string | null
  current_phase: string | null
  phase_durations: Record<string, unknown> | null
  agent_metrics: Record<string, unknown> | null
  retry_count: number | null
  error_summary: string | null
}

export type MonitorStatus = {
  websocket_connected: boolean
  time_since_connect_seconds: number | null
  last_fill_at: string | null
  breach_active: boolean
  breach_rule: string | null
}

export type AlertSummary = {
  alert_id: string
  rule_name: string
  severity: string
  fired_at: string
  context_json: string
}

export type LiveViewData = {
  pipeline: InvocationStatus
  monitor: MonitorStatus
  active_alerts: AlertSummary[]
  assembled_at: string
}

export type ScheduleTrigger = {
  trigger_at: string | null
  trigger_type: string | null
}

export type ScheduleViewData = {
  paused: boolean
  triggers: ScheduleTrigger[]
  cached_at: string | null
}

const LIVE_VIEW_QUERY_KEY = ['views', 'live'] as const
const SCHEDULE_VIEW_QUERY_KEY = ['views', 'schedule'] as const

export function liveViewQueryOptions(): ReturnType<typeof queryOptions<LiveViewData>> {
  return queryOptions<LiveViewData>({
    queryKey: LIVE_VIEW_QUERY_KEY,
    queryFn: () => api.get<LiveViewData>('/api/views/live'),
    staleTime: 5 * 1000,
    retry: 1,
  })
}

export function scheduleViewQueryOptions(): ReturnType<typeof queryOptions<ScheduleViewData>> {
  return queryOptions<ScheduleViewData>({
    queryKey: SCHEDULE_VIEW_QUERY_KEY,
    queryFn: () => api.get<ScheduleViewData>('/api/views/schedule'),
    staleTime: 10 * 1000,
    retry: 1,
  })
}

export function useLiveView(): UseQueryResult<LiveViewData> {
  return useQuery(liveViewQueryOptions())
}

export function useScheduleView(): UseQueryResult<ScheduleViewData> {
  return useQuery(scheduleViewQueryOptions())
}

// ---------------------------------------------------------------------------
// Run history hooks (story 05c / ALP-673)
// ---------------------------------------------------------------------------

function _appendFilters(params: URLSearchParams, filters: RunHistoryFilters): void {
  if (filters.date_from) {
    params.set('date_from', filters.date_from)
  }
  if (filters.date_to) {
    params.set('date_to', filters.date_to)
  }
  if (filters.run_type) {
    params.set('run_type', filters.run_type)
  }
  for (const s of filters.status ?? []) {
    params.append('status', s)
  }
  if (filters.commands_gt !== undefined) {
    params.set('commands_gt', String(filters.commands_gt))
  }
  if (filters.has_errors !== undefined) {
    params.set('has_errors', String(filters.has_errors))
  }
  params.set('page', String(filters.page ?? 1))
  params.set('page_size', String(filters.page_size ?? 50))
}

function _buildRunsUrl(base: string, filters: RunHistoryFilters): string {
  const params = new URLSearchParams()
  _appendFilters(params, filters)
  const qs = params.toString()
  return qs ? `${base}?${qs}` : base
}

export function runHistoryQueryOptions(
  filters: RunHistoryFilters,
): ReturnType<typeof queryOptions<RunHistoryPage>> {
  return queryOptions<RunHistoryPage>({
    queryKey: ['runs', filters],
    queryFn: () => api.get<RunHistoryPage>(_buildRunsUrl('/api/views/history/runs', filters)),
    staleTime: 30 * 1000,
  })
}

export function useRunHistory(filters: RunHistoryFilters): UseQueryResult<RunHistoryPage> {
  return useQuery(runHistoryQueryOptions(filters))
}

export function runHistoryFailureLogQueryOptions(
  filters: RunHistoryFilters,
): ReturnType<typeof queryOptions<RunHistoryPage>> {
  return queryOptions<RunHistoryPage>({
    queryKey: ['runs', 'failure-log', filters],
    queryFn: () =>
      api.get<RunHistoryPage>(_buildRunsUrl('/api/views/history/runs/preset/failure-log', filters)),
    staleTime: 30 * 1000,
  })
}

export function useRunHistoryFailureLog(
  filters: RunHistoryFilters,
): UseQueryResult<RunHistoryPage> {
  return useQuery(runHistoryFailureLogQueryOptions(filters))
}
