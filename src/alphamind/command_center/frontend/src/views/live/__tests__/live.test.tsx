import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { AlertBannerLive } from '../alert-banner'
import { MonitorPane } from '../monitor-pane'
import { PipelinePane } from '../pipeline-pane'
import { SchedulePreview } from '../schedule-preview'

// Unit tests for the live view components (story 05b / ALP-672).
//
// Strategy: mock `useEventStream` (no-op) + mock TanStack Query state via
// dependency injection (QueryClient with seeded state). Each component test
// asserts documented field rendering + action-button behavior.

// Silence hook subscription noise in tests.
vi.mock('@/api/events', () => ({
  useEventStream: (): void => undefined,
  SSE_EVENT_NAMES: [],
}))

// Mock the api client so POST calls don't hit a real server.
vi.mock('@/api/client', () => ({
  api: {
    post: vi.fn().mockResolvedValue({ status: 'accepted' }),
    get: vi.fn().mockResolvedValue({}),
  },
  ApiError: class ApiError extends Error {
    status: number
    detail: unknown
    constructor(status: number, detail: unknown) {
      super(`API ${String(status)}`)
      this.status = status
      this.detail = detail
    }
  },
  readCookie: vi.fn().mockReturnValue(null),
}))

type WrapperProps = { children: React.ReactNode }

function buildQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
}

function Wrapper({ children }: WrapperProps): React.JSX.Element {
  return <QueryClientProvider client={buildQueryClient()}>{children}</QueryClientProvider>
}

function renderWithClient(ui: React.ReactElement): void {
  render(ui, { wrapper: Wrapper })
}

function renderWithData(ui: React.ReactElement, qc: QueryClient): void {
  render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

// ---------------------------------------------------------------------------
// Shared mock data
// ---------------------------------------------------------------------------

const MOCK_LIVE_DATA = {
  pipeline: {
    invocation_id: 'inv-test-001',
    run_type: 'scheduled',
    started_at: '2026-05-26T09:30:00Z',
    ended_at: null,
    status: 'running',
    current_phase: 'phase1',
    phase_durations: null,
    agent_metrics: null,
    retry_count: null,
    error_summary: null,
  },
  monitor: {
    websocket_connected: true,
    time_since_connect_seconds: 42,
    last_fill_at: '2026-05-26T09:31:00Z',
    breach_active: false,
    breach_rule: null,
  },
  active_alerts: [
    {
      alert_id: 'alert-001',
      rule_name: 'high_drawdown',
      severity: 'warning',
      fired_at: '2026-05-26T09:00:00Z',
      context_json: '{}',
    },
  ],
  assembled_at: '2026-05-26T09:31:05Z',
}

const MOCK_SCHEDULE_DATA = {
  paused: false,
  triggers: [
    { trigger_at: '2026-05-26T14:00:00Z', trigger_type: 'pre_close' },
    { trigger_at: '2026-05-27T09:30:00Z', trigger_type: 'pre_open' },
  ],
  cached_at: '2026-05-26T09:30:01Z',
}

// ---------------------------------------------------------------------------
// Named assertion helpers — avoid triple-nested arrow callbacks
// ---------------------------------------------------------------------------

async function expectText(pattern: RegExp | string): Promise<void> {
  await waitFor(() => {
    expect(screen.getByText(pattern)).toBeInTheDocument()
  })
}

async function expectConnectedBadge(): Promise<void> {
  // ConnectionBadge renders "Connected"; "Connected for" is a separate <dt>.
  // getAllByText avoids "found multiple elements" ambiguity.
  await waitFor(() => {
    const matches = screen.getAllByText(/connected/i)
    expect(matches.length).toBeGreaterThan(0)
  })
}

// ---------------------------------------------------------------------------
// PipelinePane — rendering
// ---------------------------------------------------------------------------

describe('PipelinePane rendering', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders "No invocation data" when live query is loading', async () => {
    renderWithClient(<PipelinePane />)
    await expectText(/no invocation data/i)
  })

  it('renders invocation data when live query resolves', async () => {
    const qc = buildQueryClient()
    qc.setQueryData(['views', 'live'], MOCK_LIVE_DATA)
    renderWithData(<PipelinePane />, qc)
    await expectText('inv-test-001')
    await expectText('scheduled')
    await expectText('running')
  })

  it('renders action buttons', () => {
    renderWithClient(<PipelinePane />)
    expect(screen.getByRole('button', { name: /pause/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /trigger emergency/i })).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// PipelinePane — action buttons
// ---------------------------------------------------------------------------

describe('PipelinePane actions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('opens confirmation modal on Pause click', async () => {
    renderWithClient(<PipelinePane />)
    await userEvent.click(screen.getByRole('button', { name: /pause/i }))
    expect(screen.getByRole('heading', { name: /^pause$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /confirm/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /cancel/i })).toBeInTheDocument()
  })

  it('closes modal on Cancel', async () => {
    renderWithClient(<PipelinePane />)
    await userEvent.click(screen.getByRole('button', { name: /pause/i }))
    await userEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(screen.queryByRole('heading', { name: /^pause$/i })).not.toBeInTheDocument()
  })

  it('emergency modal requires typed reason before confirming', async () => {
    renderWithClient(<PipelinePane />)
    await userEvent.click(screen.getByRole('button', { name: /trigger emergency/i }))
    const confirmBtn = screen.getByRole('button', { name: /confirm/i })
    expect(confirmBtn).toBeDisabled()
    const input = screen.getByPlaceholderText(/reason/i)
    await userEvent.type(input, 'market gap event')
    expect(confirmBtn).not.toBeDisabled()
  })
})

// ---------------------------------------------------------------------------
// MonitorPane
// ---------------------------------------------------------------------------

describe('MonitorPane', () => {
  it('renders disconnected state when no data', async () => {
    renderWithClient(<MonitorPane />)
    await expectText(/disconnected/i)
  })

  it('renders connected state from live query data', async () => {
    const qc = buildQueryClient()
    qc.setQueryData(['views', 'live'], MOCK_LIVE_DATA)
    renderWithData(<MonitorPane />, qc)
    await expectConnectedBadge()
  })

  it('renders breach active state', async () => {
    const qc = buildQueryClient()
    qc.setQueryData(['views', 'live'], {
      ...MOCK_LIVE_DATA,
      monitor: { ...MOCK_LIVE_DATA.monitor, breach_active: true, breach_rule: 'daily_drawdown' },
    })
    renderWithData(<MonitorPane />, qc)
    await expectText(/active.*daily_drawdown/i)
  })
})

// ---------------------------------------------------------------------------
// AlertBannerLive
// ---------------------------------------------------------------------------

describe('AlertBannerLive', () => {
  it('renders null when no active alerts', () => {
    const qc = buildQueryClient()
    qc.setQueryData(['views', 'live'], { ...MOCK_LIVE_DATA, active_alerts: [] })
    const { container } = render(
      <QueryClientProvider client={qc}>
        <AlertBannerLive />
      </QueryClientProvider>,
    )
    // Data is already seeded — render is synchronous; no waitFor needed.
    expect(container.firstChild).toBeNull()
  })

  it('renders alert chip when active alerts present', async () => {
    const qc = buildQueryClient()
    qc.setQueryData(['views', 'live'], MOCK_LIVE_DATA)
    renderWithData(<AlertBannerLive />, qc)
    await expectText(/high_drawdown/i)
  })
})

// ---------------------------------------------------------------------------
// SchedulePreview
// ---------------------------------------------------------------------------

describe('SchedulePreview', () => {
  it('shows loading state initially', () => {
    renderWithClient(<SchedulePreview />)
    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it('renders triggers from query data', async () => {
    const qc = buildQueryClient()
    qc.setQueryData(['views', 'schedule'], MOCK_SCHEDULE_DATA)
    renderWithData(<SchedulePreview />, qc)
    await expectText('2026-05-26T14:00:00Z')
    await expectText('2026-05-27T09:30:00Z')
  })

  it('shows paused badge when schedule is paused', async () => {
    const qc = buildQueryClient()
    qc.setQueryData(['views', 'schedule'], { ...MOCK_SCHEDULE_DATA, paused: true })
    renderWithData(<SchedulePreview />, qc)
    await expectText(/paused/i)
  })

  it('shows empty state message when no triggers and not loading', async () => {
    const qc = buildQueryClient()
    qc.setQueryData(['views', 'schedule'], { paused: false, triggers: [], cached_at: null })
    renderWithData(<SchedulePreview />, qc)
    await expectText(/no upcoming triggers/i)
  })
})
