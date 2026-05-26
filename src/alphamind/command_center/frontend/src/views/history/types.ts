// Shared types for the run history view (story 05c / ALP-673).
//
// RunRow mirrors the backend RunRow Pydantic model.
// RunHistoryFilters mirrors the query-param shape of
//   GET /api/views/history/runs.

export type RunRow = {
  invocation_id: string
  started_at: string
  ended_at: string | null
  duration_seconds: number | null
  run_type: string
  status: string
  commands_submitted: number
  commands_rejected: number
  abort_reason: string | null
}

export type RunHistoryPage = {
  items: RunRow[]
  total: number
  page: number
  page_size: number
}

export type RunHistoryFilters = {
  date_from?: string
  date_to?: string
  run_type?: string
  status?: string[]
  commands_gt?: number
  has_errors?: boolean
  page?: number
  page_size?: number
}
