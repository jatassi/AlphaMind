// Vitest unit tests for portfolio dashboard pane components (ALP-676).
// Each pane is rendered in isolation with minimal fixture data;
// the TanStack Router context is provided via createMemoryRouter helpers
// for the PositionsTable (which renders deep-link <Link> elements).

import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  RouterProvider,
} from '@tanstack/react-router'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type {
  CashAndCapital,
  EquityAndPL,
  Exposure,
  PendingOrderRow,
  PositionRow,
} from '@/api/portfolio'
import { CashCapitalPane } from '@/views/portfolio/dashboard/cash-capital-pane'
import { EquityPlPane } from '@/views/portfolio/dashboard/equity-pl-pane'
import { ExposurePane } from '@/views/portfolio/dashboard/exposure-pane'
import { PendingOrdersTable } from '@/views/portfolio/dashboard/pending-orders-table'
import { PositionsTable } from '@/views/portfolio/dashboard/positions-table'

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const EQUITY: EquityAndPL = {
  total_value_usd: '100000.00',
  high_water_mark_usd: '105000.00',
  current_drawdown_pct: 4.76,
  daily_realized_pl_usd: '200.00',
  cumulative_realized_pl_usd: '5000.00',
  total_unrealized_pl_usd: '-1000.00',
}

const CASH: CashAndCapital = {
  cash_usd: '50000.00',
  settled_cash_usd: '48000.00',
  reserved_capital_usd: '10000.00',
  available_buying_power_usd: '38000.00',
  margin_held_usd: '2000.00',
  unsettled_proceeds: [
    { settlement_date: '2026-05-28', amount_usd: '1500.00', source_transaction_id: 'tx-1' },
  ],
  regt_excess: {
    trailing_30d_usd: '3000.00',
    trailing_90d_usd: '8000.00',
    lifetime_usd: '12000.00',
  },
}

const EXPOSURE: Exposure = {
  gross_exposure_pct: 120,
  net_long_pct: 60,
  net_short_pct: -40,
  delta_adjusted_net_usd: '20000.00',
  sector_breakdown: [
    { sector: 'Technology', long_market_value_usd: '40000.00', short_market_value_usd: '0.00' },
  ],
}

const POSITION: PositionRow = {
  position_id: 'pos-1',
  ticker: 'AAPL',
  instrument_type: 'EQUITY',
  direction: 'LONG',
  quantity: 100,
  market_value_usd: '18000.00',
  unrealized_pl_usd: '500.00',
  thesis_status: 'ACTIVE',
  age_hours: 48,
  distance_to_target_pct: 5,
  distance_to_nearest_invalidation_pct: -3,
}

const ORDER: PendingOrderRow = {
  order_id: 'ord-1',
  status: 'PENDING',
  instrument_spec: { ticker: 'AAPL' },
  order_type: 'LIMIT',
  order_role: 'ENTRY',
  quantity: 50,
  filled_quantity: 0,
  remaining_quantity: 50,
  direction: 'BUY',
  age_hours: 2.5,
  position_id: 'pos-1',
}

// ---------------------------------------------------------------------------
// Router wrapper for components that render <Link> (PositionsTable)
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
// EquityPlPane
// ---------------------------------------------------------------------------

describe('EquityPlPane', () => {
  it('renders total value', () => {
    render(<EquityPlPane data={EQUITY} />)
    expect(screen.getByText('Equity & P/L')).toBeInTheDocument()
    expect(screen.getByText('$100,000.00')).toBeInTheDocument()
  })

  it('renders drawdown percentage', () => {
    render(<EquityPlPane data={EQUITY} />)
    expect(screen.getByText('4.76%')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// CashCapitalPane
// ---------------------------------------------------------------------------

describe('CashCapitalPane', () => {
  it('renders cash balance', () => {
    render(<CashCapitalPane data={CASH} />)
    expect(screen.getByText('Cash & Capital')).toBeInTheDocument()
    expect(screen.getByText('$50,000.00')).toBeInTheDocument()
  })

  it('renders unsettled proceeds', () => {
    render(<CashCapitalPane data={CASH} />)
    expect(screen.getByText('Unsettled proceeds')).toBeInTheDocument()
    expect(screen.getByText('2026-05-28')).toBeInTheDocument()
  })

  it('renders Reg T trailing windows', () => {
    render(<CashCapitalPane data={CASH} />)
    expect(screen.getByText('Trailing 30d')).toBeInTheDocument()
    expect(screen.getByText('Trailing 90d')).toBeInTheDocument()
    expect(screen.getByText('Lifetime')).toBeInTheDocument()
  })

  it('renders empty unsettled when no proceeds', () => {
    const cash = { ...CASH, unsettled_proceeds: [] }
    render(<CashCapitalPane data={cash} />)
    expect(screen.queryByText('Unsettled proceeds')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// ExposurePane
// ---------------------------------------------------------------------------

describe('ExposurePane', () => {
  it('renders gross/net exposure', () => {
    render(<ExposurePane data={EXPOSURE} />)
    expect(screen.getByText('Exposure')).toBeInTheDocument()
    expect(screen.getByText('120.0%')).toBeInTheDocument()
    expect(screen.getByText('60.0%')).toBeInTheDocument()
  })

  it('renders sector breakdown when present', () => {
    render(<ExposurePane data={EXPOSURE} />)
    expect(screen.getByText('Sector breakdown')).toBeInTheDocument()
    expect(screen.getByText('Technology')).toBeInTheDocument()
  })

  it('omits sector table when breakdown is empty', () => {
    const exposure = { ...EXPOSURE, sector_breakdown: [] }
    render(<ExposurePane data={exposure} />)
    expect(screen.queryByText('Sector breakdown')).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// PositionsTable
// ---------------------------------------------------------------------------

describe('PositionsTable', () => {
  it('renders position ticker with deep-link', async () => {
    render(withRouter(<PositionsTable positions={[POSITION]} />))
    const link = await screen.findByRole('link', { name: 'AAPL' })
    expect(link).toBeInTheDocument()
  })

  it('renders empty state when no positions', async () => {
    render(withRouter(<PositionsTable positions={[]} />))
    expect(await screen.findByText('No open positions.')).toBeInTheDocument()
  })

  it('shows count in header', async () => {
    render(withRouter(<PositionsTable positions={[POSITION]} />))
    expect(await screen.findByText('Positions (1)')).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// PendingOrdersTable
// ---------------------------------------------------------------------------

describe('PendingOrdersTable', () => {
  it('renders order id', () => {
    render(<PendingOrdersTable orders={[ORDER]} />)
    expect(screen.getByText('ord-1')).toBeInTheDocument()
  })

  it('renders empty state when no orders', () => {
    render(<PendingOrdersTable orders={[]} />)
    expect(screen.getByText('No pending orders.')).toBeInTheDocument()
  })

  it('shows count in header', () => {
    render(<PendingOrdersTable orders={[ORDER]} />)
    expect(screen.getByText('Pending orders (1)')).toBeInTheDocument()
  })
})
