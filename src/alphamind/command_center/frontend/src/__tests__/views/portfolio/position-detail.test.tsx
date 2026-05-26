// Vitest unit tests for position detail view components (ALP-677).
// Tests cover: StateTab, ThesisTab, HistoryTab, ForceCloseModal.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type {
  ActivityLogEntry,
  BracketLegDetail,
  FillDetail,
  PositionDetail,
  ThesisDetail,
} from '@/api/portfolio'
import { ForceCloseModal } from '@/views/portfolio/position-detail/force-close-modal'
import { HistoryTab } from '@/views/portfolio/position-detail/history-tab'
import { StateTab } from '@/views/portfolio/position-detail/state-tab'
import { ThesisTab } from '@/views/portfolio/position-detail/thesis-tab'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const BRACKET_LEG: BracketLegDetail = {
  bracket_leg_id: 'leg-1',
  leg_index: 0,
  leg_type: 'PRICE_STOP',
  trigger_kind: 'PRICE',
  trigger_payload: { trigger_price: '200.00' },
  pl_anchor: null,
  enforcement: 'MECHANICAL',
  leg_status: 'ACTIVE',
  order_id: null,
}

const FILL: FillDetail = {
  fill_id: 'fill-1',
  order_id: 'ord-1',
  fill_timestamp: '2026-05-20T09:01:00Z',
  fill_price: '220.00',
  fill_quantity: 50,
  remaining_quantity_after: 0,
  order_status_after: 'FILLED',
  slippage_usd: null,
  fees_usd: '1.50',
  execution_venue: null,
}

const THESIS: ThesisDetail = {
  thesis_id: 'ths-1',
  status: 'ACTIVE',
  summary: 'Bullish on TSLA momentum',
  time_expectation_hours: 72,
  position_size_rationale: 'Small size given uncertainty',
  generation_timestamp: '2026-05-20T08:55:00Z',
  resolution_timestamp: null,
  resolution_category: null,
  components: [
    {
      component_id: 'comp-1',
      component_type: 'ENTRY_RATIONALE',
      linked_bracket_leg: null,
      instrument_reference: null,
      narrative: 'Strong breakout above resistance',
      key_assumptions: ['Volume confirmation', 'RSI > 60'],
      supporting_signals: ['signal:momentum'],
      resolution_outcome: null,
      resolution_notes: null,
    },
  ],
}

const LOG_ENTRY: ActivityLogEntry = {
  entry_id: 'log-1',
  invocation_id: 'inv-1',
  entry_at: '2026-05-20T09:01:05Z',
  event_type: 'POSITION_OPENED',
  event_group: 'POSITION_LIFECYCLE',
  order_id: null,
  thesis_id: null,
  source: 'FILL_PROCESSOR',
  detail_json: '{"description":"TSLA LONG opened"}',
}

const POSITION: PositionDetail = {
  position_id: 'pos-1',
  ticker: 'TSLA',
  instrument_type: 'EQUITY',
  direction: 'LONG',
  status: 'OPEN',
  quantity: 50,
  market_value_usd: '12000.00',
  unrealized_pl_usd: '750.00',
  realized_pl_usd: null,
  age_hours: 24,
  distance_to_target_pct: 8.5,
  distance_to_nearest_invalidation_pct: -4.2,
  bracket_id: 'brk-1',
  bracket_legs: [BRACKET_LEG],
  fills: [FILL],
  thesis: THESIS,
  activity_log: [LOG_ENTRY],
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function withQueryClient(child: React.ReactNode): React.ReactElement {
  return <QueryClientProvider client={makeQueryClient()}>{child}</QueryClientProvider>
}

function renderModal(onClose = vi.fn(), onSuccess = vi.fn()): void {
  render(
    withQueryClient(
      <ForceCloseModal positionId="pos-1" ticker="TSLA" onClose={onClose} onSuccess={onSuccess} />,
    ),
  )
}

// ---------------------------------------------------------------------------
// StateTab
// ---------------------------------------------------------------------------

describe('StateTab', () => {
  it('renders ticker and direction', () => {
    render(<StateTab data={POSITION} />)
    expect(screen.getByText('TSLA')).toBeInTheDocument()
    expect(screen.getByText('LONG')).toBeInTheDocument()
  })

  it('renders market value', () => {
    render(<StateTab data={POSITION} />)
    expect(screen.getByText('$12,000.00')).toBeInTheDocument()
  })

  it('renders bracket legs count header', () => {
    render(<StateTab data={POSITION} />)
    expect(screen.getByText('Bracket legs (1)')).toBeInTheDocument()
  })

  it('renders bracket leg type and status', () => {
    render(<StateTab data={POSITION} />)
    expect(screen.getByText('PRICE_STOP')).toBeInTheDocument()
    expect(screen.getByText('ACTIVE')).toBeInTheDocument()
  })

  it('renders fill history count header', () => {
    render(<StateTab data={POSITION} />)
    expect(screen.getByText('Fill history (1)')).toBeInTheDocument()
  })

  it('renders fill price', () => {
    render(<StateTab data={POSITION} />)
    expect(screen.getByText('$220.00')).toBeInTheDocument()
  })

  it('shows empty state for no bracket legs', () => {
    render(<StateTab data={{ ...POSITION, bracket_legs: [] }} />)
    expect(screen.getByText('No bracket legs.')).toBeInTheDocument()
  })

  it('shows empty state for no fills', () => {
    render(<StateTab data={{ ...POSITION, fills: [] }} />)
    expect(screen.getByText('No fills.')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// ThesisTab
// ---------------------------------------------------------------------------

describe('ThesisTab', () => {
  it('renders null thesis message', () => {
    render(<ThesisTab thesis={null} />)
    expect(screen.getByText(/no thesis linked/i)).toBeInTheDocument()
  })

  it('renders thesis summary', () => {
    render(<ThesisTab thesis={THESIS} />)
    expect(screen.getByText('Bullish on TSLA momentum')).toBeInTheDocument()
  })

  it('renders thesis status', () => {
    render(<ThesisTab thesis={THESIS} />)
    expect(screen.getByText('ACTIVE')).toBeInTheDocument()
  })

  it('renders component type', () => {
    render(<ThesisTab thesis={THESIS} />)
    expect(screen.getByText('ENTRY_RATIONALE')).toBeInTheDocument()
  })

  it('renders key assumptions', () => {
    render(<ThesisTab thesis={THESIS} />)
    expect(screen.getByText('Volume confirmation')).toBeInTheDocument()
    expect(screen.getByText('RSI > 60')).toBeInTheDocument()
  })

  it('renders supporting signals', () => {
    render(<ThesisTab thesis={THESIS} />)
    expect(screen.getByText('signal:momentum')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// HistoryTab — pre-filters by position_id
// ---------------------------------------------------------------------------

describe('HistoryTab', () => {
  it('renders activity log entries', () => {
    render(<HistoryTab positionId="pos-1" entries={[LOG_ENTRY]} />)
    expect(screen.getByText('POSITION_OPENED')).toBeInTheDocument()
    expect(screen.getByText('FILL_PROCESSOR')).toBeInTheDocument()
  })

  it('renders position id in header', () => {
    render(<HistoryTab positionId="pos-1" entries={[LOG_ENTRY]} />)
    expect(screen.getByText('pos-1')).toBeInTheDocument()
  })

  it('shows empty state when no entries', () => {
    render(<HistoryTab positionId="pos-1" entries={[]} />)
    expect(screen.getByText(/no activity log entries/i)).toBeInTheDocument()
  })

  it('renders detail summary', () => {
    render(<HistoryTab positionId="pos-1" entries={[LOG_ENTRY]} />)
    expect(screen.getByText('TSLA LONG opened')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// ForceCloseModal — typed-token confirmation gate
// ---------------------------------------------------------------------------

describe('ForceCloseModal — render', () => {
  it('renders ticker in confirmation prompt', () => {
    renderModal()
    expect(screen.getAllByText('TSLA').length).toBeGreaterThan(0)
  })

  it('submit button is disabled initially', () => {
    renderModal()
    expect(screen.getByRole('button', { name: /force close/i })).toBeDisabled()
  })

  it('cancel button calls onClose', async () => {
    const onClose = vi.fn()
    renderModal(onClose)
    await userEvent.setup().click(screen.getByRole('button', { name: /cancel/i }))
    expect(onClose).toHaveBeenCalledOnce()
  })
})

describe('ForceCloseModal — typed-token gate', () => {
  it('submit disabled without rationale even when ticker matches', async () => {
    const user = userEvent.setup()
    renderModal()
    await user.type(screen.getByLabelText(/type tsla to confirm/i), 'TSLA')
    expect(screen.getByRole('button', { name: /force close/i })).toBeDisabled()
  })

  it('submit enables when ticker and rationale are both filled', async () => {
    const user = userEvent.setup()
    renderModal()
    await user.type(screen.getByLabelText(/type tsla to confirm/i), 'TSLA')
    await user.type(screen.getByLabelText(/rationale/i), 'Emergency exit needed')
    expect(screen.getByRole('button', { name: /force close/i })).not.toBeDisabled()
  })

  it('ticker match is case-insensitive', async () => {
    const user = userEvent.setup()
    renderModal()
    await user.type(screen.getByLabelText(/type tsla to confirm/i), 'tsla')
    await user.type(screen.getByLabelText(/rationale/i), 'Test rationale')
    expect(screen.getByRole('button', { name: /force close/i })).not.toBeDisabled()
  })
})
