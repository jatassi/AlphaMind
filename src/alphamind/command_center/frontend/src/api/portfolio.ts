// Portfolio API types and query hooks (ALP-676 dashboard, ALP-677 detail).
//
// Types mirror the Pydantic response models in
// `command_center/views/portfolio.py`.  Until `bun run generate-types`
// can run against a live daemon, types are defined inline here.

import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query'

import { api } from './client'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type UnsettledProceedsItem = {
  settlement_date: string
  amount_usd: string
  source_transaction_id: string
}

export type RegTExcess = {
  trailing_30d_usd: string
  trailing_90d_usd: string
  lifetime_usd: string
}

export type EquityAndPL = {
  total_value_usd: string
  high_water_mark_usd: string
  current_drawdown_pct: number
  daily_realized_pl_usd: string
  cumulative_realized_pl_usd: string
  total_unrealized_pl_usd: string
}

export type CashAndCapital = {
  cash_usd: string
  settled_cash_usd: string
  reserved_capital_usd: string
  available_buying_power_usd: string
  margin_held_usd: string
  unsettled_proceeds: UnsettledProceedsItem[]
  regt_excess: RegTExcess
}

export type SectorExposureItem = {
  sector: string
  long_market_value_usd: string
  short_market_value_usd: string
}

export type Exposure = {
  gross_exposure_pct: number
  net_long_pct: number
  net_short_pct: number
  sector_breakdown: SectorExposureItem[]
  delta_adjusted_net_usd: string
}

export type PositionRow = {
  position_id: string
  ticker: string
  instrument_type: string
  direction: string | null
  quantity: number
  market_value_usd: string
  unrealized_pl_usd: string
  thesis_status: string | null
  age_hours: number
  distance_to_target_pct: number | null
  distance_to_nearest_invalidation_pct: number | null
}

export type PendingOrderRow = {
  order_id: string
  status: string
  instrument_spec: Record<string, unknown>
  order_type: string
  order_role: string
  quantity: number
  filled_quantity: number
  remaining_quantity: number
  direction: string | null
  age_hours: number
  position_id: string | null
}

export type PortfolioDashboard = {
  equity_and_pl: EquityAndPL
  cash_and_capital: CashAndCapital
  exposure: Exposure
  positions: PositionRow[]
  pending_orders: PendingOrderRow[]
}

// ---------------------------------------------------------------------------
// Query
// ---------------------------------------------------------------------------

const PORTFOLIO_DASHBOARD_KEY = ['portfolio', 'dashboard'] as const

export function portfolioDashboardQueryOptions() {
  return queryOptions<PortfolioDashboard>({
    queryKey: PORTFOLIO_DASHBOARD_KEY,
    queryFn: () => api.get<PortfolioDashboard>('/api/views/portfolio/dashboard'),
    staleTime: 10 * 1000, // 10 s — SSE events trigger refetch proactively
    retry: 1,
  })
}

export function usePortfolioDashboard(): UseQueryResult<PortfolioDashboard> {
  return useQuery(portfolioDashboardQueryOptions())
}

// ---------------------------------------------------------------------------
// Position detail types (ALP-677)
// ---------------------------------------------------------------------------

export type BracketLegDetail = {
  bracket_leg_id: string
  leg_index: number
  leg_type: string
  trigger_kind: string
  trigger_payload: Record<string, unknown>
  pl_anchor: Record<string, unknown> | null
  enforcement: string
  leg_status: string
  order_id: string | null
}

export type FillDetail = {
  fill_id: string
  order_id: string
  fill_timestamp: string
  fill_price: string
  fill_quantity: number
  remaining_quantity_after: number
  order_status_after: string
  slippage_usd: string | null
  fees_usd: string
  execution_venue: string | null
}

export type ThesisComponentDetail = {
  component_id: string
  component_type: string
  linked_bracket_leg: string | null
  instrument_reference: string | null
  narrative: string
  key_assumptions: string[]
  supporting_signals: string[]
  resolution_outcome: string | null
  resolution_notes: string | null
}

export type ThesisDetail = {
  thesis_id: string
  status: string
  summary: string
  time_expectation_hours: number | null
  position_size_rationale: string | null
  generation_timestamp: string
  resolution_timestamp: string | null
  resolution_category: string | null
  components: ThesisComponentDetail[]
}

export type ActivityLogEntry = {
  entry_id: string
  invocation_id: string
  entry_at: string
  event_type: string
  event_group: string
  order_id: string | null
  thesis_id: string | null
  source: string
  detail_json: string
}

export type PositionDetail = {
  position_id: string
  ticker: string
  instrument_type: string
  direction: string | null
  status: string
  quantity: number
  market_value_usd: string
  unrealized_pl_usd: string
  realized_pl_usd: string | null
  age_hours: number
  distance_to_target_pct: number | null
  distance_to_nearest_invalidation_pct: number | null
  bracket_id: string | null
  bracket_legs: BracketLegDetail[]
  fills: FillDetail[]
  thesis: ThesisDetail | null
  activity_log: ActivityLogEntry[]
}

// ---------------------------------------------------------------------------
// Position detail query (ALP-677)
// ---------------------------------------------------------------------------

export function positionDetailQueryOptions(positionId: string) {
  return queryOptions<PositionDetail>({
    queryKey: ['portfolio', 'position', positionId] as const,
    queryFn: () =>
      api.get<PositionDetail>(`/api/views/portfolio/positions/${encodeURIComponent(positionId)}`),
    staleTime: 10 * 1000,
    retry: 1,
  })
}

export function usePositionDetail(positionId: string): UseQueryResult<PositionDetail> {
  return useQuery(positionDetailQueryOptions(positionId))
}
