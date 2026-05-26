import type * as ReactRouterTypes from '@tanstack/react-router'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { RunFilters, RunList } from '../run-list'
import type { RunHistoryFilters, RunRow } from '../types'

// Unit tests for run-history view components (story 05c / ALP-673).
//
// ACs verified:
//   - RunList renders the 8 documented column headers.
//   - Status chip toggles add/remove entries from the status array.
//   - Date-to filter appends T23:59:59 so ISO comparison reaches end of day.
//   - Clear-filters button fires with status: undefined.
//   - Pagination component shows correct page count and calls onPageChange.

// ---------------------------------------------------------------------------
// Minimal RunRow factory
// ---------------------------------------------------------------------------

function makeRow(overrides: Partial<RunRow> = {}): RunRow {
  return {
    invocation_id: 'inv-001',
    started_at: '2026-05-01T10:00:00',
    ended_at: '2026-05-01T10:05:00',
    duration_seconds: 300,
    run_type: 'scheduled',
    status: 'completed',
    commands_submitted: 4,
    commands_rejected: 0,
    abort_reason: null,
    ...overrides,
  }
}

function emptyFilters(): RunHistoryFilters {
  return { page: 1, page_size: 50 }
}

// ---------------------------------------------------------------------------
// RunList — column headers
// ---------------------------------------------------------------------------

// RunList calls useNavigate internally; stub it while preserving all other
// exports from the real module so route/root imports don't break.
vi.mock('@tanstack/react-router', async (importOriginal) => {
  const actual = await importOriginal<typeof ReactRouterTypes>()
  return { ...actual, useNavigate: () => vi.fn() }
})

const EXPECTED_HEADERS = [
  'Invocation ID',
  'Started at',
  'Duration',
  'Run type',
  'Status',
  '# Commands',
  '# Rejections',
  'Abort reason',
]

describe('RunList — column headers', () => {
  it('renders all 8 documented column headers', () => {
    render(
      <RunList
        rows={[makeRow()]}
        isLoading={false}
        isPreset
        filters={emptyFilters()}
        onFilterChange={vi.fn()}
      />,
    )
    for (const header of EXPECTED_HEADERS) {
      expect(screen.getByText(header)).toBeInTheDocument()
    }
  })
})

describe('RunList — loading and empty states', () => {
  it('shows "Loading…" when isLoading is true', () => {
    render(
      <RunList rows={[]} isLoading isPreset filters={emptyFilters()} onFilterChange={vi.fn()} />,
    )
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows empty message when no rows and not loading', () => {
    render(
      <RunList
        rows={[]}
        isLoading={false}
        isPreset
        filters={emptyFilters()}
        onFilterChange={vi.fn()}
      />,
    )
    expect(screen.getByText('No runs match the current filters.')).toBeInTheDocument()
  })

  it('renders one table row per RunRow in data', () => {
    const rows = [makeRow({ invocation_id: 'a' }), makeRow({ invocation_id: 'b' })]
    render(
      <RunList
        rows={rows}
        isLoading={false}
        isPreset
        filters={emptyFilters()}
        onFilterChange={vi.fn()}
      />,
    )
    expect(screen.getByText('a')).toBeInTheDocument()
    expect(screen.getByText('b')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// RunFilters — filter state
// ---------------------------------------------------------------------------

describe('RunFilters status chips', () => {
  it('adds a status when a chip is clicked while inactive', () => {
    const onChange = vi.fn()
    render(<RunFilters filters={emptyFilters()} onFilterChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'completed' }))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ status: ['completed'] }))
  })

  it('removes a status when the active chip is clicked again', () => {
    const onChange = vi.fn()
    render(
      <RunFilters
        filters={{ ...emptyFilters(), status: ['completed', 'failed'] }}
        onFilterChange={onChange}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'completed' }))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ status: ['failed'] }))
  })

  it('sets status to undefined when last chip is deactivated', () => {
    const onChange = vi.fn()
    render(
      <RunFilters filters={{ ...emptyFilters(), status: ['partial'] }} onFilterChange={onChange} />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'partial' }))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ status: undefined }))
  })
})

describe('RunFilters Clear filters button', () => {
  it('shows Clear filters button only when status is active', () => {
    const { rerender } = render(<RunFilters filters={emptyFilters()} onFilterChange={vi.fn()} />)
    expect(screen.queryByText('Clear filters')).toBeNull()
    rerender(
      <RunFilters filters={{ ...emptyFilters(), status: ['failed'] }} onFilterChange={vi.fn()} />,
    )
    expect(screen.getByText('Clear filters')).toBeInTheDocument()
  })

  it('Clear filters calls onFilterChange with status: undefined', () => {
    const onChange = vi.fn()
    render(
      <RunFilters filters={{ ...emptyFilters(), status: ['failed'] }} onFilterChange={onChange} />,
    )
    fireEvent.click(screen.getByText('Clear filters'))
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ status: undefined }))
  })
})

describe('RunFilters date inputs', () => {
  it('passes date_from as-is to onFilterChange', () => {
    const onChange = vi.fn()
    render(<RunFilters filters={emptyFilters()} onFilterChange={onChange} />)
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-05-01' } })
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ date_from: '2026-05-01' }))
  })

  it('appends T23:59:59 to date_to so ISO comparison reaches end of day', () => {
    const onChange = vi.fn()
    render(<RunFilters filters={emptyFilters()} onFilterChange={onChange} />)
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-05-01' } })
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ date_to: '2026-05-01T23:59:59' }),
    )
  })

  it('sets date_from to undefined when input is cleared', () => {
    const onChange = vi.fn()
    render(
      <RunFilters
        filters={{ ...emptyFilters(), date_from: '2026-05-01' }}
        onFilterChange={onChange}
      />,
    )
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '' } })
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ date_from: undefined }))
  })
})

// ---------------------------------------------------------------------------
// Pagination — show/hide + page callbacks
// ---------------------------------------------------------------------------

// Pagination is exported from index-page; import it directly.
import { Pagination } from '@/routes/_authed/history/index-page'

describe('Pagination', () => {
  it('shows correct page count label', () => {
    render(<Pagination page={2} pageSize={10} total={35} onPageChange={vi.fn()} />)
    expect(screen.getByText('Page 2 of 4')).toBeInTheDocument()
  })

  it('Previous button is disabled on page 1', () => {
    render(<Pagination page={1} pageSize={10} total={25} onPageChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
  })

  it('Next button is disabled on the last page', () => {
    render(<Pagination page={3} pageSize={10} total={25} onPageChange={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
  })

  it('Previous button calls onPageChange with page - 1', () => {
    const onChange = vi.fn()
    render(<Pagination page={3} pageSize={10} total={35} onPageChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Previous' }))
    expect(onChange).toHaveBeenCalledWith(2)
  })

  it('Next button calls onPageChange with page + 1', () => {
    const onChange = vi.fn()
    render(<Pagination page={2} pageSize={10} total={35} onPageChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(onChange).toHaveBeenCalledWith(3)
  })
})
