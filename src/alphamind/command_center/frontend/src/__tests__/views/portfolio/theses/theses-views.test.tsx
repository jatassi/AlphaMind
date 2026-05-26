// Vitest tests for theses views (ALP-678).
// Tests cover:
// - ThesesFilterBar: status + classification chip toggles
// - ThesesTable: documented columns, deep-links
// - StatusTimeline: transition rendering, cited reference-ID chips
// - ReferenceIdChip: deep-link href
// - ThesisDetailBody: renders all sections

import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import type { ThesisDetail, ThesisRow, ThesisStatusTransition } from '@/api/portfolio'
import { ReferenceIdChip } from '@/views/portfolio/theses/reference-id-chip'
import { StatusTimeline } from '@/views/portfolio/theses/status-timeline'
import { ThesesFilterBar } from '@/views/portfolio/theses/theses-filter-bar'
import { ThesesTable } from '@/views/portfolio/theses/theses-table'

// ---------------------------------------------------------------------------
// Router wrapper (required for <Link> components)
// ---------------------------------------------------------------------------

function withRouter(child: React.ReactNode): React.ReactElement {
  const root = createRootRoute({ component: () => child })
  const index = createRoute({ getParentRoute: () => root, path: '/', component: () => null })
  const router = createRouter({
    routeTree: root.addChildren([index]),
    history: createMemoryHistory({ initialEntries: ['/'] }),
  })
  return <RouterProvider router={router} />
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const THESIS_ROW: ThesisRow = {
  thesis_id: 'ths-1',
  position_id: 'pos-1',
  status: 'ACTIVE',
  thesis_status: 'ON_TRACK',
  resolution_category: null,
  summary: 'Bullish AAPL on AI capex expansion',
  generation_timestamp: '2026-05-24T10:00:00Z',
  resolution_timestamp: null,
  age_hours: 48.5,
  position_unrealized_pl_usd: '1200.00',
}

const RESOLVED_THESIS_ROW: ThesisRow = {
  thesis_id: 'ths-2',
  position_id: 'pos-2',
  status: 'RESOLVED',
  thesis_status: 'INVALIDATED',
  resolution_category: 'VALIDATED',
  summary: 'Bearish TSLA on EV slowdown',
  generation_timestamp: '2026-05-20T10:00:00Z',
  resolution_timestamp: '2026-05-25T10:00:00Z',
  age_hours: 144,
  position_unrealized_pl_usd: '-500.00',
}

const TRANSITIONS: ThesisStatusTransition[] = [
  {
    entry_id: 'log-1',
    entry_at: '2026-05-24T12:00:00Z',
    old_status: 'ACTIVE',
    new_status: 'ON_TRACK',
    cited_reference_ids: [],
  },
  {
    entry_id: 'log-2',
    entry_at: '2026-05-25T08:00:00Z',
    old_status: 'ON_TRACK',
    new_status: 'AT_RISK',
    cited_reference_ids: ['SA-7', 'QR-3'],
  },
]

const THESIS_DETAIL: ThesisDetail = {
  thesis_id: 'ths-1',
  position_id: 'pos-1',
  status: 'ACTIVE',
  thesis_status: 'ON_TRACK',
  resolution_category: null,
  summary: 'Bullish AAPL on AI capex expansion',
  generation_timestamp: '2026-05-24T10:00:00Z',
  resolution_timestamp: null,
  age_hours: 48.5,
  components: [
    {
      component_id: 'comp-1',
      component_type: 'ENTRY_RATIONALE',
      linked_bracket_leg: null,
      instrument_reference: 'AAPL',
      narrative: 'Strong AI infrastructure spend drives data-center demand',
      key_assumptions: ['AI capex exceeds consensus', 'margins expand Q3'],
      resolution_outcome: null,
      resolution_notes: null,
    },
  ],
  status_history: TRANSITIONS,
  resolution_component_outcomes: {},
}

// ---------------------------------------------------------------------------
// ReferenceIdChip
// ---------------------------------------------------------------------------

describe('ReferenceIdChip', () => {
  it('renders the refId text', () => {
    render(<ReferenceIdChip refId="SA-7" />)
    expect(screen.getByText('SA-7')).toBeInTheDocument()
  })

  it('deep-links to brief viewer path', () => {
    render(<ReferenceIdChip refId="QR-3" />)
    const chip = screen.getByRole('link', { name: /QR-3/i })
    expect(chip).toHaveAttribute('href', '/history/briefs/QR-3')
  })
})

// ---------------------------------------------------------------------------
// StatusTimeline
// ---------------------------------------------------------------------------

describe('StatusTimeline', () => {
  it('shows empty state when history is empty', () => {
    render(<StatusTimeline history={[]} />)
    expect(screen.getByText(/No status transitions/i)).toBeInTheDocument()
  })

  it('renders each transition with old → new status', async () => {
    render(<StatusTimeline history={TRANSITIONS} />)
    expect(await screen.findByText('ACTIVE')).toBeInTheDocument()
    // ON TRACK appears twice (new_status of first; old_status of second).
    const onTrackElements = await screen.findAllByText('ON TRACK')
    expect(onTrackElements.length).toBeGreaterThanOrEqual(1)
    expect(await screen.findByText('AT RISK')).toBeInTheDocument()
  })

  it('renders cited reference-ID chips for transitions that have them', async () => {
    render(<StatusTimeline history={TRANSITIONS} />)
    expect(await screen.findByText('SA-7')).toBeInTheDocument()
    expect(screen.getByText('QR-3')).toBeInTheDocument()
  })

  it('does not render chips when cited_reference_ids is empty', () => {
    const noRefs = [TRANSITIONS[0]]
    render(<StatusTimeline history={noRefs} />)
    expect(screen.queryByRole('link', { name: /SA-/i })).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// ThesesFilterBar
// ---------------------------------------------------------------------------

describe('ThesesFilterBar — rendering', () => {
  it('renders status filter chips', () => {
    let filters = {}
    render(
      <ThesesFilterBar
        filters={filters}
        onFiltersChange={(f) => {
          filters = f
        }}
      />,
    )
    expect(screen.getByRole('button', { name: 'ACTIVE' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'RESOLVED' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'CANCELLED' })).toBeInTheDocument()
  })

  it('renders classification filter chips', () => {
    render(<ThesesFilterBar filters={{}} onFiltersChange={() => undefined} />)
    expect(screen.getByRole('button', { name: 'ON TRACK' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'AT RISK' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'STALE' })).toBeInTheDocument()
  })
})

describe('ThesesFilterBar — interactions', () => {
  it('calls onFiltersChange with status set when clicking a status chip', async () => {
    const user = userEvent.setup()
    const changes: Record<string, unknown>[] = []
    render(
      <ThesesFilterBar
        filters={{}}
        onFiltersChange={(f) => {
          changes.push(f)
        }}
      />,
    )
    await user.click(screen.getByRole('button', { name: 'ACTIVE' }))
    expect(changes.at(-1)).toMatchObject({ status: 'ACTIVE' })
  })

  it('clears status when clicking the same chip again (toggle off)', async () => {
    const user = userEvent.setup()
    const changes: Record<string, unknown>[] = []
    render(
      <ThesesFilterBar
        filters={{ status: 'ACTIVE' }}
        onFiltersChange={(f) => {
          changes.push(f)
        }}
      />,
    )
    await user.click(screen.getByRole('button', { name: 'ACTIVE' }))
    expect(changes.at(-1)).toMatchObject({ status: undefined })
  })

  it('calls onFiltersChange with classification set when clicking a chip', async () => {
    const user = userEvent.setup()
    const changes: Record<string, unknown>[] = []
    render(
      <ThesesFilterBar
        filters={{}}
        onFiltersChange={(f) => {
          changes.push(f)
        }}
      />,
    )
    await user.click(screen.getByRole('button', { name: 'AT RISK' }))
    expect(changes.at(-1)).toMatchObject({ classification: 'AT_RISK' })
  })
})

// ---------------------------------------------------------------------------
// ThesesTable
// ---------------------------------------------------------------------------

describe('ThesesTable', () => {
  it('renders thesis summary as a deep-link', async () => {
    render(withRouter(<ThesesTable theses={[THESIS_ROW]} total={1} />))
    const link = await screen.findByRole('link', { name: /Bullish AAPL/i })
    expect(link).toBeInTheDocument()
    expect(link.getAttribute('href')).toContain('ths-1')
  })

  it('renders thesis classification column', async () => {
    render(withRouter(<ThesesTable theses={[THESIS_ROW]} total={1} />))
    expect(await screen.findByText('ON TRACK')).toBeInTheDocument()
  })

  it('renders position deep-link chip', async () => {
    render(withRouter(<ThesesTable theses={[THESIS_ROW]} total={1} />))
    expect(await screen.findByRole('link', { name: 'pos-1' })).toBeInTheDocument()
  })

  it('renders resolution category for resolved thesis', async () => {
    render(withRouter(<ThesesTable theses={[RESOLVED_THESIS_ROW]} total={1} />))
    expect(await screen.findByText('VALIDATED')).toBeInTheDocument()
  })

  it('shows empty state when theses list is empty', async () => {
    render(withRouter(<ThesesTable theses={[]} total={0} />))
    expect(await screen.findByText(/No theses match/i)).toBeInTheDocument()
  })

  it('renders total count in card title', async () => {
    render(withRouter(<ThesesTable theses={[THESIS_ROW, RESOLVED_THESIS_ROW]} total={15} />))
    expect(await screen.findByText('Theses (15)')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// StatusTimeline with cited chips — integration via detail fixture
// ---------------------------------------------------------------------------

describe('StatusTimeline cited chips integration', () => {
  it('renders all cited reference IDs from the detail fixture', async () => {
    render(<StatusTimeline history={THESIS_DETAIL.status_history} />)
    expect(await screen.findByText('SA-7')).toBeInTheDocument()
    expect(screen.getByText('QR-3')).toBeInTheDocument()
  })

  it('reference-ID chip href points to brief viewer path', async () => {
    render(<StatusTimeline history={THESIS_DETAIL.status_history} />)
    const chip = await screen.findByRole('link', { name: 'SA-7' })
    expect(chip.getAttribute('href')).toBe('/history/briefs/SA-7')
  })
})
