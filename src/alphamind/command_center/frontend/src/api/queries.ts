import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { RunHistoryFilters, RunHistoryPage } from '@/views/history/types'

import { api, ApiError } from './client'

// ── Activity log types ─────────────────────────────────────────────────────

export type ActivityLogRow = {
  entry_id: string
  invocation_id: string
  entry_at: string
  event_type: string
  event_group: string
  position_id: string | null
  order_id: string | null
  thesis_id: string | null
  source: string
  detail_json: string
}

export type ActivityLogPage = {
  rows: ActivityLogRow[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

export type ActivityLogFilters = {
  event_type?: string[]
  invocation_id?: string
  position_id?: string
  thesis_id?: string
  order_id?: string
  source?: string[]
  time_from?: string
  time_to?: string
  page?: number
  page_size?: number
}

export type EventTypesResponse = {
  event_types: string[]
}

export type SavedFilter = {
  name: string
  description: string
  // Optional values — a preset may not set every dimension.
  params: Partial<Record<string, string[]>>
}

export type SavedFiltersResponse = {
  saved_filters: SavedFilter[]
}

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

// ── Activity log hooks (story 05e / ALP-675) ────────────────────────────────

function appendMultiParam(
  params: URLSearchParams,
  key: string,
  values: string[] | undefined,
): void {
  for (const v of values ?? []) {
    params.append(key, v)
  }
}

function setIfPresent(params: URLSearchParams, key: string, value: string | undefined): void {
  if (value) {
    params.set(key, value)
  }
}

function buildActivityLogQueryString(filters: ActivityLogFilters): string {
  const params = new URLSearchParams()
  appendMultiParam(params, 'event_type', filters.event_type)
  appendMultiParam(params, 'source', filters.source)
  setIfPresent(params, 'invocation_id', filters.invocation_id)
  setIfPresent(params, 'position_id', filters.position_id)
  setIfPresent(params, 'thesis_id', filters.thesis_id)
  setIfPresent(params, 'order_id', filters.order_id)
  setIfPresent(params, 'time_from', filters.time_from)
  setIfPresent(params, 'time_to', filters.time_to)
  if (filters.page !== undefined) {
    params.set('page', String(filters.page))
  }
  if (filters.page_size !== undefined) {
    params.set('page_size', String(filters.page_size))
  }
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export function activityLogQueryOptions(
  filters: ActivityLogFilters,
): ReturnType<typeof queryOptions<ActivityLogPage>> {
  return queryOptions<ActivityLogPage>({
    queryKey: ['activity-log', filters],
    queryFn: () =>
      api.get<ActivityLogPage>(`/api/views/activity-log${buildActivityLogQueryString(filters)}`),
    staleTime: 10 * 1000,
  })
}

export function useActivityLog(filters: ActivityLogFilters): UseQueryResult<ActivityLogPage> {
  return useQuery(activityLogQueryOptions(filters))
}

export function eventTypesQueryOptions(): ReturnType<typeof queryOptions<EventTypesResponse>> {
  return queryOptions<EventTypesResponse>({
    queryKey: ['activity-log-event-types'],
    queryFn: () => api.get<EventTypesResponse>('/api/views/activity-log/event-types'),
    staleTime: 5 * 60 * 1000,
  })
}

export function useEventTypes(): UseQueryResult<EventTypesResponse> {
  return useQuery(eventTypesQueryOptions())
}

export function savedFiltersQueryOptions(): ReturnType<typeof queryOptions<SavedFiltersResponse>> {
  return queryOptions<SavedFiltersResponse>({
    queryKey: ['activity-log-saved-filters'],
    queryFn: () => api.get<SavedFiltersResponse>('/api/views/activity-log/saved-filters'),
    staleTime: 5 * 60 * 1000,
  })
}

export function useSavedFilters(): UseQueryResult<SavedFiltersResponse> {
  return useQuery(savedFiltersQueryOptions())
}

// ---------------------------------------------------------------------------
// Invocation detail hooks (story 05d / ALP-674)
// ---------------------------------------------------------------------------

export type ArchiveSection = {
  section: string
  files: string[]
}

export type InvocationDetailHeader = {
  invocation_id: string
  run_type: string
  started_at: string
  ended_at: string | null
  status: string
  fill_collection_completed_at: string | null
  command_execution_completed_at: string | null
  duration_seconds: number | null
  trigger_type: string
  trigger_reason: string
  git_sha: string
  active_profile: string
  active_regime: string
  active_mode: string
  abort_reason: string | null
  error_summary: string | null
}

export type InvocationActivityEntry = {
  entry_id: string
  entry_at: string
  event_type: string
  event_group: string
  position_id: string | null
  order_id: string | null
  thesis_id: string | null
  source: string
  detail_json: string
}

export type InvocationDetailResponse = {
  header: InvocationDetailHeader
  archive_root: string | null
  archive_sections: ArchiveSection[]
  pm_entries: InvocationActivityEntry[]
  command_fill_entries: InvocationActivityEntry[]
}

export type BriefSection = {
  ref_id: string
  content: string
}

export type BriefRetrievalResponse = {
  invocation_id: string
  ref_prefix: string
  sections: BriefSection[]
}

export function invocationDetailQueryOptions(
  invocationId: string,
): ReturnType<typeof queryOptions<InvocationDetailResponse>> {
  return queryOptions<InvocationDetailResponse>({
    queryKey: ['invocation-detail', invocationId],
    queryFn: () =>
      api.get<InvocationDetailResponse>(
        `/api/views/history/runs/${encodeURIComponent(invocationId)}`,
      ),
    staleTime: 60 * 1000,
    retry: 1,
  })
}

export function useInvocationDetail(
  invocationId: string,
): UseQueryResult<InvocationDetailResponse> {
  return useQuery(invocationDetailQueryOptions(invocationId))
}

export function briefRetrievalQueryOptions(
  invocationId: string,
  refPrefix: string,
): ReturnType<typeof queryOptions<BriefRetrievalResponse>> {
  const params = new URLSearchParams({
    invocation_id: invocationId,
    ref_prefix: refPrefix,
  })
  return queryOptions<BriefRetrievalResponse>({
    queryKey: ['brief-retrieval', invocationId, refPrefix],
    queryFn: () =>
      api.get<BriefRetrievalResponse>(`/api/views/history/brief-retrieval?${params.toString()}`),
    staleTime: 5 * 60 * 1000,
    retry: 1,
  })
}

export function useBriefRetrieval(
  invocationId: string,
  refPrefix: string,
): UseQueryResult<BriefRetrievalResponse> {
  return useQuery(briefRetrievalQueryOptions(invocationId, refPrefix))
}
