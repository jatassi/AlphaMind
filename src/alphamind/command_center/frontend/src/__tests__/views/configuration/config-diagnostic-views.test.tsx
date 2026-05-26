// Vitest tests for the config diagnostic view components (ALP-684 story 06c).
//
// ResolvedConfigPage  — loading / error states, bundle display, source-file
//                       collapsible sections, diff-modal open/close.
// ConfigHistoryPage   — file picker, commits list, diff pane, status badge.
// DiffModal           — input fields, compare button, empty-diff message.
//
// All API hooks are mocked via vi.mock so tests run without a server.

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  useGitDiff,
  useGitHistory,
  useGitStatus,
  useResolvedConfig,
  useResolvedConfigDiff,
} from '@/api/config-views'
import { ConfigHistoryPage } from '@/views/configuration/history/history-page'
import { DiffModal } from '@/views/configuration/resolved/diff-modal'
import { ResolvedConfigPage } from '@/views/configuration/resolved/resolved-config-page'

// ---------------------------------------------------------------------------
// Mock the API hooks
// ---------------------------------------------------------------------------

vi.mock('@/api/config-views', () => ({
  useResolvedConfig: vi.fn(),
  useResolvedConfigDiff: vi.fn(),
  useGitHistory: vi.fn(),
  useGitDiff: vi.fn(),
  useGitStatus: vi.fn(),
}))

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const BUNDLE_FIXTURE = {
  invocation_id: 'inv-abc123',
  bundle: { profile: 'default', regime: 'normal', mode: 'live' },
  source_files: {
    'profiles/default.yaml': 'name: default\n',
    'regimes/normal.yaml': 'label: Normal\n',
  },
}

const DIFF_FIXTURE = {
  from_invocation_id: 'inv-old',
  to_invocation_id: 'inv-new',
  diff_lines: [
    '--- inv-old/resolved_config.json',
    '+++ inv-new/resolved_config.json',
    '@@ -1,3 +1,3 @@',
    ' { "profile": "default",',
    '-  "regime": "normal"',
    '+  "regime": "high-vol"',
    ' }',
  ],
}

const COMMITS_FIXTURE = [
  {
    sha: 'aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111',
    short_sha: 'aaaa111',
    author: 'Alice',
    date: '2026-05-20',
    subject: 'tighten leverage limit',
  },
  {
    sha: 'bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222',
    short_sha: 'bbbb222',
    author: 'Bob',
    date: '2026-05-15',
    subject: 'initial guardrails commit',
  },
]

const GIT_STATUS_TRACKED = {
  file: 'guardrails.yaml',
  tracked: true,
  has_uncommitted_changes: false,
  untracked: false,
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function pending() {
  return { data: undefined, isPending: true, isError: false }
}

function error() {
  return { data: undefined, isPending: false, isError: true }
}

function ok<T>(data: T) {
  return { data, isPending: false, isError: false }
}

// ---------------------------------------------------------------------------
// ResolvedConfigPage — loading + error states
// ---------------------------------------------------------------------------

describe('ResolvedConfigPage loading state', () => {
  beforeEach(() => {
    vi.mocked(useResolvedConfig).mockReturnValue(pending() as never)
  })

  it('shows loading indicator while data is pending', () => {
    render(<ResolvedConfigPage />)
    expect(screen.getByText(/loading resolved config/i)).toBeInTheDocument()
  })
})

describe('ResolvedConfigPage error state', () => {
  beforeEach(() => {
    vi.mocked(useResolvedConfig).mockReturnValue(error() as never)
  })

  it('shows error message on failure', () => {
    render(<ResolvedConfigPage />)
    expect(screen.getByText(/failed to load resolved config/i)).toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// ResolvedConfigPage — data loaded
// ---------------------------------------------------------------------------

describe('ResolvedConfigPage with data', () => {
  beforeEach(() => {
    vi.mocked(useResolvedConfig).mockReturnValue(ok(BUNDLE_FIXTURE) as never)
    vi.mocked(useResolvedConfigDiff).mockReturnValue(pending() as never)
  })

  it('renders the invocation_id', () => {
    render(<ResolvedConfigPage />)
    expect(screen.getByText('inv-abc123')).toBeInTheDocument()
  })

  it('renders the bundle JSON block', () => {
    render(<ResolvedConfigPage />)
    expect(screen.getByText(/resolved bundle/i)).toBeInTheDocument()
    expect(screen.getByText(/"profile"/)).toBeInTheDocument()
  })

  it('renders source file paths as collapsible sections', () => {
    render(<ResolvedConfigPage />)
    expect(screen.getByText('profiles/default.yaml')).toBeInTheDocument()
    expect(screen.getByText('regimes/normal.yaml')).toBeInTheDocument()
  })

  it('expands a source file section when clicked', async () => {
    const user = userEvent.setup()
    render(<ResolvedConfigPage />)
    await user.click(screen.getByRole('button', { name: /profiles\/default.yaml/i }))
    expect(screen.getByText('name: default')).toBeInTheDocument()
  })

  it('shows diff-with-previous button', () => {
    render(<ResolvedConfigPage />)
    expect(screen.getByRole('button', { name: /diff with previous/i })).toBeInTheDocument()
  })

  it('opens DiffModal when diff button is clicked', async () => {
    const user = userEvent.setup()
    render(<ResolvedConfigPage />)
    await user.click(screen.getByRole('button', { name: /diff with previous/i }))
    expect(screen.getByText(/diff resolved configs/i)).toBeInTheDocument()
  })

  it('closes DiffModal when close button is clicked', async () => {
    const user = userEvent.setup()
    render(<ResolvedConfigPage />)
    await user.click(screen.getByRole('button', { name: /diff with previous/i }))
    await user.click(screen.getByRole('button', { name: /close diff modal/i }))
    expect(screen.queryByText(/diff resolved configs/i)).not.toBeInTheDocument()
  })
})

// ---------------------------------------------------------------------------
// DiffModal standalone
// ---------------------------------------------------------------------------

describe('DiffModal', () => {
  beforeEach(() => {
    vi.mocked(useResolvedConfigDiff).mockReturnValue(pending() as never)
  })

  it('pre-fills the to-id input with defaultToId', () => {
    render(<DiffModal defaultToId="inv-new" onClose={vi.fn()} />)
    expect(screen.getByLabelText(/to invocation id/i)).toHaveValue('inv-new')
  })

  it('shows prompt text before comparison is triggered', () => {
    render(<DiffModal defaultToId="inv-new" onClose={vi.fn()} />)
    expect(screen.getByText(/enter two invocation ids/i)).toBeInTheDocument()
  })

  it('shows diff after Compare is clicked with both IDs filled', async () => {
    vi.mocked(useResolvedConfigDiff).mockReturnValue(ok(DIFF_FIXTURE) as never)
    const user = userEvent.setup()
    render(<DiffModal defaultToId="inv-new" onClose={vi.fn()} />)
    await user.type(screen.getByLabelText(/from invocation id/i), 'inv-old')
    await user.click(screen.getByRole('button', { name: /compare/i }))
    const regimeEl = await screen.findByText(/high-vol/)
    expect(regimeEl).toBeInTheDocument()
  })

  it('disables Compare button when from-id is empty', () => {
    render(<DiffModal defaultToId="inv-new" onClose={vi.fn()} />)
    expect(screen.getByRole('button', { name: /compare/i })).toBeDisabled()
  })

  it('shows empty-diff message when diff_lines is empty', async () => {
    vi.mocked(useResolvedConfigDiff).mockReturnValue(
      ok({ ...DIFF_FIXTURE, diff_lines: [] }) as never,
    )
    const user = userEvent.setup()
    render(<DiffModal defaultToId="inv-new" onClose={vi.fn()} />)
    await user.type(screen.getByLabelText(/from invocation id/i), 'inv-old')
    await user.click(screen.getByRole('button', { name: /compare/i }))
    const emptyMsg = await screen.findByText(/no changes between/i)
    expect(emptyMsg).toBeInTheDocument()
  })

  it('calls onClose when close button is pressed', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<DiffModal defaultToId="inv-new" onClose={onClose} />)
    await user.click(screen.getByRole('button', { name: /close diff modal/i }))
    expect(onClose).toHaveBeenCalledOnce()
  })
})

// ---------------------------------------------------------------------------
// ConfigHistoryPage
// ---------------------------------------------------------------------------

describe('ConfigHistoryPage commits list', () => {
  beforeEach(() => {
    vi.mocked(useGitHistory).mockReturnValue(
      ok({ file: 'guardrails.yaml', commits: COMMITS_FIXTURE }) as never,
    )
    vi.mocked(useGitStatus).mockReturnValue(ok(GIT_STATUS_TRACKED) as never)
    vi.mocked(useGitDiff).mockReturnValue(pending() as never)
  })

  it('renders commit subjects', () => {
    render(<ConfigHistoryPage />)
    expect(screen.getByText('tighten leverage limit')).toBeInTheDocument()
    expect(screen.getByText('initial guardrails commit')).toBeInTheDocument()
  })

  it('renders short SHAs', () => {
    render(<ConfigHistoryPage />)
    expect(screen.getByText('aaaa111')).toBeInTheDocument()
    expect(screen.getByText('bbbb222')).toBeInTheDocument()
  })

  it('shows tracked badge for a tracked file', () => {
    render(<ConfigHistoryPage />)
    expect(screen.getByText('tracked')).toBeInTheDocument()
  })
})

describe('ConfigHistoryPage empty history', () => {
  beforeEach(() => {
    vi.mocked(useGitHistory).mockReturnValue(ok({ file: 'new.yaml', commits: [] }) as never)
    vi.mocked(useGitStatus).mockReturnValue(
      ok({
        file: 'new.yaml',
        tracked: false,
        has_uncommitted_changes: false,
        untracked: true,
      }) as never,
    )
    vi.mocked(useGitDiff).mockReturnValue(pending() as never)
  })

  it('shows empty-history message', () => {
    render(<ConfigHistoryPage />)
    expect(screen.getByText(/no commits found/i)).toBeInTheDocument()
  })

  it('shows untracked badge', () => {
    render(<ConfigHistoryPage />)
    expect(screen.getByText('untracked')).toBeInTheDocument()
  })
})

describe('ConfigHistoryPage diff pane', () => {
  beforeEach(() => {
    vi.mocked(useGitHistory).mockReturnValue(
      ok({ file: 'guardrails.yaml', commits: COMMITS_FIXTURE }) as never,
    )
    vi.mocked(useGitStatus).mockReturnValue(ok(GIT_STATUS_TRACKED) as never)
    vi.mocked(useGitDiff).mockReturnValue(pending() as never)
  })

  it('shows prompt when no from/to selected', () => {
    render(<ConfigHistoryPage />)
    expect(screen.getByText(/select a.*from.*and.*to.*commit/i)).toBeInTheDocument()
  })

  it('shows diff after selecting from and to commits', async () => {
    vi.mocked(useGitDiff).mockReturnValue(
      ok({
        file: 'guardrails.yaml',
        from_sha: COMMITS_FIXTURE[1].sha,
        to_sha: COMMITS_FIXTURE[0].sha,
        diff_text: '-old_limit: 5\n+new_limit: 3\n',
      }) as never,
    )
    const user = userEvent.setup()
    render(<ConfigHistoryPage />)
    const fromButtons = screen.getAllByRole('button', { name: /from/i })
    const toButtons = screen.getAllByRole('button', { name: /to/i })
    await user.click(fromButtons[1])
    await user.click(toButtons[0])
    const diffEl = await screen.findByText(/-old_limit: 5/)
    expect(diffEl).toBeInTheDocument()
  })
})

describe('ConfigHistoryPage loading + error states', () => {
  it('shows loading text while history is pending', () => {
    vi.mocked(useGitHistory).mockReturnValue(pending() as never)
    vi.mocked(useGitStatus).mockReturnValue(pending() as never)
    vi.mocked(useGitDiff).mockReturnValue(pending() as never)
    render(<ConfigHistoryPage />)
    expect(screen.getByText(/loading history/i)).toBeInTheDocument()
  })

  it('shows error text when history fails', () => {
    vi.mocked(useGitHistory).mockReturnValue(error() as never)
    vi.mocked(useGitStatus).mockReturnValue(ok(GIT_STATUS_TRACKED) as never)
    vi.mocked(useGitDiff).mockReturnValue(pending() as never)
    render(<ConfigHistoryPage />)
    expect(screen.getByText(/failed to load git history/i)).toBeInTheDocument()
  })
})
