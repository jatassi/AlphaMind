// Risk views API types and query hooks (ALP-680).
//
// Types mirror the Pydantic response models in
// `command_center/views/risk.py`. Three query hooks:
//   useGuardrailDashboard()  — GET /api/views/risk/guardrail-dashboard
//   useRegimeTimeline(from, to) — GET /api/views/risk/regime-timeline
//   useCalibrationMix(window) — GET /api/views/risk/calibration-mix

import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query'

import { api } from './client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type RuleStatus = {
  rule_name: string
  current_value: number
  limit_value: number
  headroom_pct: number
  zone: 'normal' | 'warning' | 'critical' | 'hard_block'
}

export type MultiplierEntry = {
  rule_name: string
  multiplier: number
  effective_limit: number
}

export type ActiveMultipliersAndOverlays = {
  regime_label: string | null
  multipliers: MultiplierEntry[]
  active_overlays: string[]
  halt_mode_engaged: boolean
}

export type DrawdownStatus = {
  current_drawdown_pct: number
  equity_high_water_mark_usd: string
  daily_drawdown_pct: number
  daily_drawdown_limit_pct: number
  daily_zone: string
  cumulative_drawdown_pct: number
  cumulative_drawdown_limit_pct: number
  cumulative_zone: string
  progressive_tier: number
  halt_mode_engaged: boolean
}

export type RecentBreachItem = {
  entry_id: string
  entry_at: string
  event_type: string
  position_id: string | null
  detail_json: string
}

export type GuardrailDashboard = {
  rules: RuleStatus[]
  active_multipliers_and_overlays: ActiveMultipliersAndOverlays
  drawdown: DrawdownStatus
  recent_breaches: RecentBreachItem[]
}

export type RegimeTimelineEvent = {
  entry_at: string
  event_type: string
  event_group: string
  detail_json: string
  position_id: string | null
}

export type RegimeTimeline = {
  events: RegimeTimelineEvent[]
  from_ts: string
  to_ts: string
}

export type CalibrationStateCount = {
  invocation_id: string
  as_of: string
  total_blocks: number
  calibrated: number
  accumulating: number
  unavailable: number
  calibrated_pct: number
}

export type StuckBlockEntry = {
  block_id: string
  reason: string
  last_calibrated_invocation_id: string | null
}

export type SevenDayTrendPoint = {
  date: string
  calibrated_pct: number
  total_blocks: number
}

export type CalibrationMix = {
  per_invocation: CalibrationStateCount[]
  seven_day_trend: SevenDayTrendPoint[]
  stuck_blocks: StuckBlockEntry[]
  warmup_duration_estimate: string
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

const GUARDRAIL_DASHBOARD_KEY = ['risk', 'guardrail-dashboard'] as const
const REGIME_TIMELINE_KEY = (from: string, to: string) =>
  ['risk', 'regime-timeline', from, to] as const
const CALIBRATION_MIX_KEY = (window: number) => ['risk', 'calibration-mix', window] as const

export function guardrailDashboardQueryOptions() {
  return queryOptions<GuardrailDashboard>({
    queryKey: GUARDRAIL_DASHBOARD_KEY,
    queryFn: () => api.get<GuardrailDashboard>('/api/views/risk/guardrail-dashboard'),
    staleTime: 15 * 1000,
    retry: 1,
  })
}

export function regimeTimelineQueryOptions(from: string, to: string) {
  const qs = new URLSearchParams({ from, to }).toString()
  return queryOptions<RegimeTimeline>({
    queryKey: REGIME_TIMELINE_KEY(from, to),
    queryFn: () => api.get<RegimeTimeline>(`/api/views/risk/regime-timeline?${qs}`),
    staleTime: 30 * 1000,
    retry: 1,
  })
}

export function calibrationMixQueryOptions(invocationsWindow = 20) {
  const qs = new URLSearchParams({
    invocations_window: String(invocationsWindow),
  }).toString()
  return queryOptions<CalibrationMix>({
    queryKey: CALIBRATION_MIX_KEY(invocationsWindow),
    queryFn: () => api.get<CalibrationMix>(`/api/views/risk/calibration-mix?${qs}`),
    staleTime: 60 * 1000,
    retry: 1,
  })
}

export function useGuardrailDashboard(): UseQueryResult<GuardrailDashboard> {
  return useQuery(guardrailDashboardQueryOptions())
}

export function useRegimeTimeline(from: string, to: string): UseQueryResult<RegimeTimeline> {
  return useQuery(regimeTimelineQueryOptions(from, to))
}

export function useCalibrationMix(invocationsWindow = 20): UseQueryResult<CalibrationMix> {
  return useQuery(calibrationMixQueryOptions(invocationsWindow))
}
