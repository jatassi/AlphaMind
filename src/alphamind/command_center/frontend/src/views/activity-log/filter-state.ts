// Filter state helpers for the activity-log explorer view.
//
// URL search-param serialisation: filter state is represented as plain record
// so TanStack Router's `search` schema can drive the URL. Each filter dimension
// maps to a named param; multi-value dimensions use arrays.

import type { ActivityLogFilters } from '@/api/queries'

export type FilterState = {
  event_type: string[]
  source: string[]
  invocation_id: string
  position_id: string
  thesis_id: string
  order_id: string
  time_from: string
  time_to: string
  page: number
  page_size: number
}

export const DEFAULT_FILTER_STATE: FilterState = {
  event_type: [],
  source: [],
  invocation_id: '',
  position_id: '',
  thesis_id: '',
  order_id: '',
  time_from: '',
  time_to: '',
  page: 1,
  page_size: 50,
}

export function filterStateToApiFilters(state: FilterState): ActivityLogFilters {
  return {
    event_type: state.event_type.length > 0 ? state.event_type : undefined,
    source: state.source.length > 0 ? state.source : undefined,
    invocation_id: state.invocation_id || undefined,
    position_id: state.position_id || undefined,
    thesis_id: state.thesis_id || undefined,
    order_id: state.order_id || undefined,
    time_from: state.time_from || undefined,
    time_to: state.time_to || undefined,
    page: state.page,
    page_size: state.page_size,
  }
}

function getStrings(search: Record<string, unknown>, key: string): string[] {
  const val = search[key]
  if (Array.isArray(val)) {
    return val.filter((v): v is string => typeof v === 'string')
  }
  return typeof val === 'string' && val ? [val] : []
}

function getStr(search: Record<string, unknown>, key: string): string {
  const val = search[key]
  return typeof val === 'string' ? val : ''
}

function getInt(search: Record<string, unknown>, key: string, fallback: number): number {
  const val = search[key]
  const n = Number(val)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

export function searchToFilterState(search: Record<string, unknown>): FilterState {
  return {
    event_type: getStrings(search, 'event_type'),
    source: getStrings(search, 'source'),
    invocation_id: getStr(search, 'invocation_id'),
    position_id: getStr(search, 'position_id'),
    thesis_id: getStr(search, 'thesis_id'),
    order_id: getStr(search, 'order_id'),
    time_from: getStr(search, 'time_from'),
    time_to: getStr(search, 'time_to'),
    page: getInt(search, 'page', 1),
    page_size: getInt(search, 'page_size', 50),
  }
}

function addIfNotEmpty(out: Record<string, unknown>, key: string, val: string | string[]): void {
  const isEmpty = Array.isArray(val) ? val.length === 0 : !val
  if (!isEmpty) {
    out[key] = val
  }
}

export function filterStateToSearch(state: FilterState): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  addIfNotEmpty(out, 'event_type', state.event_type)
  addIfNotEmpty(out, 'source', state.source)
  addIfNotEmpty(out, 'invocation_id', state.invocation_id)
  addIfNotEmpty(out, 'position_id', state.position_id)
  addIfNotEmpty(out, 'thesis_id', state.thesis_id)
  addIfNotEmpty(out, 'order_id', state.order_id)
  addIfNotEmpty(out, 'time_from', state.time_from)
  addIfNotEmpty(out, 'time_to', state.time_to)
  if (state.page !== 1) {
    out.page = state.page
  }
  if (state.page_size !== 50) {
    out.page_size = state.page_size
  }
  return out
}

// Toggle a value in a multi-select list.
export function toggleInList(list: string[], value: string): string[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value]
}
