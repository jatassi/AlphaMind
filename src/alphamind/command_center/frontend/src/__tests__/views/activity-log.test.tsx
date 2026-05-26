// Vitest tests for the activity-log explorer view (story 05e / ALP-675).

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ActivityLogTable } from '@/views/activity-log/activity-log-table'
import { DetailPanel } from '@/views/activity-log/detail-panel'
import { ChipGroup } from '@/views/activity-log/filter-chips'
import {
  filterStateToSearch,
  searchToFilterState,
  toggleInList,
} from '@/views/activity-log/filter-state'
import { SavedFilterSidebar } from '@/views/activity-log/saved-filter-sidebar'

// ── Fixtures ────────────────────────────────────────────────────────────────

const SAMPLE_ROW = {
  entry_id: 'e-001',
  invocation_id: 'inv-001',
  entry_at: '2026-01-01T12:00:00Z',
  event_type: 'PM_DECISION',
  event_group: 'PM_DECISION',
  position_id: 'pos-001',
  order_id: null,
  thesis_id: 'the-001',
  source: 'COMMAND_EXECUTOR',
  detail_json: JSON.stringify({ verdict: 'APPROVE', reason: 'looks good' }),
}

function makeQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function Wrapper({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <QueryClientProvider client={makeQueryClient()}>{children}</QueryClientProvider>
}

function renderTable(isLoading = false): void {
  render(
    <Wrapper>
      <ActivityLogTable rows={isLoading ? [] : [SAMPLE_ROW]} isLoading={isLoading} />
    </Wrapper>,
  )
}

// ── ActivityLogTable — structure ──────────────────────────────────────────────

describe('ActivityLogTable columns', () => {
  it('renders documented column headers', () => {
    renderTable()
    expect(screen.getByText('Event type')).toBeInTheDocument()
    expect(screen.getByText('Timestamp')).toBeInTheDocument()
    expect(screen.getByText('Entity')).toBeInTheDocument()
    expect(screen.getByText('Source')).toBeInTheDocument()
    expect(screen.getByText('Summary')).toBeInTheDocument()
  })

  it('renders event_type in each row', () => {
    renderTable()
    expect(screen.getByText('PM_DECISION')).toBeInTheDocument()
  })

  it('shows loading state when isLoading is true', () => {
    renderTable(true)
    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it('shows empty message when no rows', () => {
    render(
      <Wrapper>
        <ActivityLogTable rows={[]} isLoading={false} />
      </Wrapper>,
    )
    expect(screen.getByText(/no rows match/i)).toBeInTheDocument()
  })
})

// ── ActivityLogTable — row expansion ─────────────────────────────────────────

describe('ActivityLogTable row expansion', () => {
  it('expands row and shows detail_json on click', async () => {
    const user = userEvent.setup()
    renderTable()
    await user.click(screen.getByRole('row', { name: /PM_DECISION/i }))
    expect(screen.getByText('Event detail')).toBeInTheDocument()
  })

  it('collapses row on second click', async () => {
    const user = userEvent.setup()
    renderTable()
    const row = screen.getByRole('row', { name: /PM_DECISION/i })
    await user.click(row)
    expect(screen.getByText('Event detail')).toBeInTheDocument()
    await user.click(row)
    expect(screen.queryByText('Event detail')).not.toBeInTheDocument()
  })
})

// ── DetailPanel ──────────────────────────────────────────────────────────────

describe('DetailPanel entity chips', () => {
  it('renders position_id chip with correct href', () => {
    render(
      <Wrapper>
        <DetailPanel row={SAMPLE_ROW} />
      </Wrapper>,
    )
    expect(screen.getByText('pos-001').closest('a')).toHaveAttribute('href', '/positions/pos-001')
  })

  it('renders thesis_id chip with correct href', () => {
    render(
      <Wrapper>
        <DetailPanel row={SAMPLE_ROW} />
      </Wrapper>,
    )
    expect(screen.getByText('the-001').closest('a')).toHaveAttribute('href', '/theses/the-001')
  })

  it('does not render order chip when order_id is null', () => {
    render(
      <Wrapper>
        <DetailPanel row={SAMPLE_ROW} />
      </Wrapper>,
    )
    expect(screen.queryAllByText('order')).toHaveLength(0)
  })

  it('renders detail JSON in pre block', () => {
    render(
      <Wrapper>
        <DetailPanel row={SAMPLE_ROW} />
      </Wrapper>,
    )
    expect(screen.getByText(/looks good/)).toBeInTheDocument()
  })
})

// ── ChipGroup ────────────────────────────────────────────────────────────────

describe('ChipGroup', () => {
  it('renders all options', () => {
    render(<ChipGroup label="Event type" options={['A', 'B']} selected={[]} onToggle={vi.fn()} />)
    expect(screen.getByText('A')).toBeInTheDocument()
    expect(screen.getByText('B')).toBeInTheDocument()
  })

  it('calls onToggle when chip is clicked', async () => {
    const user = userEvent.setup()
    const onToggle = vi.fn()
    render(<ChipGroup label="Event type" options={['A']} selected={[]} onToggle={onToggle} />)
    await user.click(screen.getByText('A'))
    expect(onToggle).toHaveBeenCalledWith('A')
  })
})

// ── SavedFilterSidebar ───────────────────────────────────────────────────────

const SAVED_FILTER_PAYLOAD = {
  saved_filters: [
    {
      name: 'Operator actions',
      description: 'From console',
      params: { source: ['OPERATOR_CONSOLE'] },
    },
  ],
}

function fakeSavedFilters(): Promise<Response> {
  const body = { ok: true, status: 200, json: () => Promise.resolve(SAVED_FILTER_PAYLOAD) }
  return Promise.resolve(body as Response)
}

describe('SavedFilterSidebar', () => {
  it('renders preset names from query data', async () => {
    vi.stubGlobal('fetch', fakeSavedFilters)
    render(
      <Wrapper>
        <SavedFilterSidebar onApply={vi.fn()} />
      </Wrapper>,
    )
    expect(await screen.findByText('Operator actions')).toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})

// ── filter-state helpers ─────────────────────────────────────────────────────

describe('filter-state', () => {
  it('toggleInList adds a value not in the list', () => {
    expect(toggleInList(['A'], 'B')).toEqual(['A', 'B'])
  })

  it('toggleInList removes a value already in the list', () => {
    expect(toggleInList(['A', 'B'], 'A')).toEqual(['B'])
  })

  it('searchToFilterState round-trips through filterStateToSearch', () => {
    const initial = {
      event_type: ['PM_DECISION'],
      source: ['OPERATOR_CONSOLE'],
      invocation_id: 'inv-001',
      position_id: '',
      thesis_id: '',
      order_id: '',
      time_from: '',
      time_to: '',
      page: 2,
      page_size: 100,
    }
    expect(searchToFilterState(filterStateToSearch(initial))).toEqual(initial)
  })

  it('filterStateToSearch omits default page and page_size', () => {
    const search = filterStateToSearch({
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
    })
    expect(search.page).toBeUndefined()
    expect(search.page_size).toBeUndefined()
  })
})

beforeEach(() => {
  vi.unstubAllGlobals()
})
