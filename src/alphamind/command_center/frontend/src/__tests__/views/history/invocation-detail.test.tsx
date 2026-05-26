// Vitest tests for the per-invocation detail view (story 05d / ALP-674).

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type {
  BriefRetrievalResponse,
  InvocationActivityEntry,
  InvocationDetailHeader,
} from '@/api/queries'
import { BriefViewer, BriefViewerPanel } from '@/views/history/brief-viewer'
import { CommandsPane } from '@/views/history/invocation-detail/commands-pane'
import { HeaderPane } from '@/views/history/invocation-detail/header-pane'
import { PmPane } from '@/views/history/invocation-detail/pm-pane'
import { NarrativeWithChips, RefIdChip } from '@/views/history/invocation-detail/ref-id-chip'
import { extractRefPrefix, isRefPrefix } from '@/views/history/invocation-detail/ref-id-utils'

// ── Fixtures ────────────────────────────────────────────────────────────────

const COMPLETED_HEADER: InvocationDetailHeader = {
  invocation_id: 'inv-test-001',
  run_type: 'pre_open',
  started_at: '2026-05-10T09:00:00',
  ended_at: '2026-05-10T09:10:00',
  status: 'completed',
  phase1_completed_at: '2026-05-10T09:05:00',
  phase2_completed_at: '2026-05-10T09:10:00',
  duration_seconds: 600,
  trigger_type: 'scheduled',
  trigger_reason: 'cron',
  git_sha: 'deadbeef',
  active_profile: 'default',
  active_regime: 'normal',
  active_mode: 'normal',
  abort_reason: null,
  error_summary: null,
}

const FAILED_HEADER: InvocationDetailHeader = {
  ...COMPLETED_HEADER,
  invocation_id: 'inv-test-002',
  status: 'failed',
  ended_at: null,
  phase1_completed_at: null,
  phase2_completed_at: null,
  duration_seconds: null,
  abort_reason: 'context-overflow',
  error_summary: 'out of tokens',
}

const PM_ENTRY: InvocationActivityEntry = {
  entry_id: 'e-pm-001',
  entry_at: '2026-05-10T09:08:00',
  event_type: 'PM_DECISION',
  event_group: 'PM_DECISION',
  position_id: null,
  order_id: null,
  thesis_id: null,
  source: 'GUARDRAIL_LAYER',
  detail_json: JSON.stringify({
    verdict: 'APPROVE',
    rationale: 'Signal [SA-TECH-3] supports entry.',
    anti_patterns: ['OVER_CONCENTRATE'],
  }),
}

const REJECT_ENTRY: InvocationActivityEntry = {
  ...PM_ENTRY,
  entry_id: 'e-pm-002',
  detail_json: JSON.stringify({
    verdict: 'REJECT',
    rationale: 'Risk too high [CR-1].',
    concerns: 'Drawdown risk elevated.',
  }),
}

const ORDER_ENTRY: InvocationActivityEntry = {
  entry_id: 'e-order-001',
  entry_at: '2026-05-10T09:09:00',
  event_type: 'ORDER_SUBMITTED',
  event_group: 'ORDER_LIFECYCLE',
  position_id: null,
  order_id: 'ord-001',
  thesis_id: null,
  source: 'COMMAND_EXECUTOR',
  detail_json: JSON.stringify({ order_id: 'ord-001' }),
}

const BRIEF_DATA: BriefRetrievalResponse = {
  invocation_id: 'inv-test-001',
  ref_prefix: 'SA-TECH',
  sections: [
    { ref_id: 'SA-TECH-1', content: 'First tech finding.' },
    { ref_id: 'SA-TECH-2', content: 'Second tech finding.' },
  ],
}

function makeQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function neverResolve(): Promise<never> {
  return new Promise(() => undefined)
}

function Wrapper({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <QueryClientProvider client={makeQueryClient()}>{children}</QueryClientProvider>
}

// ── HeaderPane ────────────────────────────────────────────────────────────────

describe('HeaderPane', () => {
  it('renders all documented fields', () => {
    render(<HeaderPane header={COMPLETED_HEADER} />)
    expect(screen.getByText('inv-test-001')).toBeInTheDocument()
    expect(screen.getByText('pre_open')).toBeInTheDocument()
    expect(screen.getByText('completed')).toBeInTheDocument()
    expect(screen.getByText('deadbeef')).toBeInTheDocument()
    expect(screen.getByText('default')).toBeInTheDocument()
    // Both regime and mode are 'normal' — assert at least one renders.
    expect(screen.getAllByText('normal').length).toBeGreaterThanOrEqual(1)
  })

  it('renders duration formatted', () => {
    render(<HeaderPane header={COMPLETED_HEADER} />)
    expect(screen.getByText('10m 0s')).toBeInTheDocument()
  })

  it('renders abort reason when present', () => {
    render(<HeaderPane header={FAILED_HEADER} />)
    expect(screen.getByText('context-overflow')).toBeInTheDocument()
  })

  it('renders error summary when present', () => {
    render(<HeaderPane header={FAILED_HEADER} />)
    expect(screen.getByText('out of tokens')).toBeInTheDocument()
  })

  it('does not render abort reason section when null', () => {
    render(<HeaderPane header={COMPLETED_HEADER} />)
    expect(screen.queryByText('Abort reason')).not.toBeInTheDocument()
  })
})

// ── PmPane ───────────────────────────────────────────────────────────────────

describe('PmPane', () => {
  it('renders empty state when no entries', () => {
    render(<PmPane entries={[]} onChipClick={() => undefined} />)
    expect(screen.getByText(/no pm decision/i)).toBeInTheDocument()
  })

  it('renders approved verdict pill', () => {
    render(<PmPane entries={[PM_ENTRY]} onChipClick={() => undefined} />)
    expect(screen.getByText('APPROVE')).toBeInTheDocument()
  })

  it('renders rejected envelope with equal prominence', () => {
    render(<PmPane entries={[REJECT_ENTRY]} onChipClick={() => undefined} />)
    expect(screen.getByText('REJECT')).toBeInTheDocument()
    expect(screen.getByText(/risk too high/i)).toBeInTheDocument()
    expect(screen.getByText('Drawdown risk elevated.')).toBeInTheDocument()
  })

  it('renders anti-pattern tags', () => {
    render(<PmPane entries={[PM_ENTRY]} onChipClick={() => undefined} />)
    expect(screen.getByText('OVER_CONCENTRATE')).toBeInTheDocument()
  })

  it('renders reference-ID chips in rationale', () => {
    render(<PmPane entries={[PM_ENTRY]} onChipClick={() => undefined} />)
    expect(screen.getByRole('button', { name: /view brief for SA-TECH-3/i })).toBeInTheDocument()
  })

  it('chip click calls onChipClick', async () => {
    const onChipClick = vi.fn()
    render(<PmPane entries={[PM_ENTRY]} onChipClick={onChipClick} />)
    const chip = screen.getByRole('button', { name: /view brief for SA-TECH-3/i })
    await userEvent.click(chip)
    expect(onChipClick).toHaveBeenCalledWith('SA-TECH-3', 'SA-TECH')
  })
})

// ── CommandsPane ──────────────────────────────────────────────────────────────

describe('CommandsPane', () => {
  it('renders empty state', () => {
    render(<CommandsPane entries={[]} />)
    expect(screen.getByText(/no command or fill/i)).toBeInTheDocument()
  })

  it('renders ORDER_SUBMITTED event', () => {
    render(<CommandsPane entries={[ORDER_ENTRY]} />)
    expect(screen.getByText('ORDER_SUBMITTED')).toBeInTheDocument()
  })

  it('renders order ID', () => {
    render(<CommandsPane entries={[ORDER_ENTRY]} />)
    expect(screen.getByText('ord-001')).toBeInTheDocument()
  })
})

// ── RefIdChip ─────────────────────────────────────────────────────────────────

describe('RefIdChip', () => {
  it('renders as a button for known prefix', () => {
    render(<RefIdChip refId="SA-TECH-3" onClick={() => undefined} />)
    expect(screen.getByRole('button', { name: /view brief for SA-TECH-3/i })).toBeInTheDocument()
  })

  it('renders as plain text for unknown prefix', () => {
    render(<RefIdChip refId="UNKNOWN-1" onClick={() => undefined} />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.getByText('UNKNOWN-1')).toBeInTheDocument()
  })

  it('calls onClick with refId and prefix', async () => {
    const onClick = vi.fn()
    render(<RefIdChip refId="QR-4" onClick={onClick} />)
    await userEvent.click(screen.getByRole('button'))
    expect(onClick).toHaveBeenCalledWith('QR-4', 'QR')
  })
})

// ── NarrativeWithChips ────────────────────────────────────────────────────────

describe('NarrativeWithChips', () => {
  it('renders chips for embedded reference IDs', () => {
    render(<NarrativeWithChips text="See [SA-TECH-1] and [QR-2]." onChipClick={() => undefined} />)
    expect(screen.getByRole('button', { name: /view brief for SA-TECH-1/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /view brief for QR-2/i })).toBeInTheDocument()
  })

  it('preserves surrounding text', () => {
    render(<NarrativeWithChips text="See [CR-1] for details." onChipClick={() => undefined} />)
    expect(screen.getByText(/see/i)).toBeInTheDocument()
    expect(screen.getByText(/for details/i)).toBeInTheDocument()
  })
})

// ── ref-id-utils ──────────────────────────────────────────────────────────────

describe('extractRefPrefix', () => {
  it('extracts SA-TECH prefix', () => {
    expect(extractRefPrefix('SA-TECH-3')).toBe('SA-TECH')
  })

  it('extracts QR prefix', () => {
    expect(extractRefPrefix('QR-4')).toBe('QR')
  })

  it('returns null for unknown prefix', () => {
    expect(extractRefPrefix('UNKNOWN-1')).toBeNull()
  })
})

describe('isRefPrefix', () => {
  it('returns true for SA-TECH', () => {
    expect(isRefPrefix('SA-TECH')).toBe(true)
  })

  it('returns false for UNKNOWN', () => {
    expect(isRefPrefix('UNKNOWN')).toBe(false)
  })
})

// ── BriefViewer ───────────────────────────────────────────────────────────────

describe('BriefViewer', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders loading state when query pending', () => {
    vi.stubGlobal('fetch', neverResolve)
    render(
      <Wrapper>
        <BriefViewer invocationId="inv-001" refPrefix="SA-TECH" />
      </Wrapper>,
    )
    expect(screen.getByText(/loading brief/i)).toBeInTheDocument()
  })

  it('renders brief sections from fixture data', async () => {
    const mockResponse = { ok: true, status: 200, json: () => Promise.resolve(BRIEF_DATA) }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(mockResponse))
    render(
      <Wrapper>
        <BriefViewer invocationId="inv-test-001" refPrefix="SA-TECH" />
      </Wrapper>,
    )
    expect(await screen.findByText('SA-TECH-1')).toBeInTheDocument()
    expect(screen.getByText('SA-TECH-2')).toBeInTheDocument()
  })
})

// ── BriefViewerPanel ──────────────────────────────────────────────────────────

describe('BriefViewerPanel', () => {
  it('renders null when refPrefix is null', () => {
    const { container } = render(
      <Wrapper>
        <BriefViewerPanel invocationId="inv-001" refPrefix={null} onClose={() => undefined} />
      </Wrapper>,
    )
    expect(container.firstChild).toBeNull()
  })

  it('renders dialog when refPrefix is provided', () => {
    vi.stubGlobal('fetch', () => new Promise(() => undefined))
    render(
      <Wrapper>
        <BriefViewerPanel invocationId="inv-001" refPrefix="SA-TECH" onClose={() => undefined} />
      </Wrapper>,
    )
    expect(screen.getByRole('dialog', { name: /brief viewer/i })).toBeInTheDocument()
  })

  it('calls onClose when close button clicked', async () => {
    vi.stubGlobal('fetch', () => new Promise(() => undefined))
    const onClose = vi.fn()
    render(
      <Wrapper>
        <BriefViewerPanel invocationId="inv-001" refPrefix="QR" onClose={onClose} />
      </Wrapper>,
    )
    await userEvent.click(screen.getByRole('button', { name: /close brief viewer/i }))
    expect(onClose).toHaveBeenCalledOnce()
  })
})
