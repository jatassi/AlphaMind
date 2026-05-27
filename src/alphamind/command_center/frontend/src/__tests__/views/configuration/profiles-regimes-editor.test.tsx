// Vitest tests for the profiles + regimes config editor pages (ALP-682).
//
// Verifies:
// * ProfileEditorPage + RegimeEditorPage render from a fixture schema.
// * FormComposer dispatches to ObjectArrayTableEditor for rule_values /
//   multipliers (object-array control type).
// * ReloadPolicyBadge renders with invocation_time for all fields.
// * Transition-discipline reminder banner renders after a successful save.
// * FilePicker sidebar renders slug names from the fixture.
// * Initial form value seeded from the GET /api/views/config/{slug}
//   payload (regression guard for empty-state DOA: pre-fix the page
//   constructed a blank ``{}`` instead of fetching real YAML, so a save
//   round-trip wiped the on-disk profile to ``{}``).

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FormSchema } from '@/views/configuration/framework/types'
import { ProfileEditorPage } from '@/views/configuration/profiles/profile-editor-page'
import { RegimeEditorPage } from '@/views/configuration/regimes/regime-editor-page'
import { FilePicker } from '@/views/configuration/shared/file-picker'

// ── Helpers ───────────────────────────────────────────────────────────────────

function buildQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function wrap(ui: React.ReactElement): React.ReactElement {
  return <QueryClientProvider client={buildQueryClient()}>{ui}</QueryClientProvider>
}

// Representative profile schema fixture — matches what the backend derives
// from ProfileConfig. Includes an object-array field (rule_values) and an
// enum field (risk_priority) to exercise the dispatcher.
const PROFILE_SCHEMA: FormSchema = {
  slug: 'profiles/small',
  filename: 'profiles/small.yaml',
  fields: [
    {
      path: 'risk_priority',
      control_type: 'enum',
      reload_policy: 'invocation_time',
      constraints: {
        enum_choices: [
          'signal_quality',
          'concentration_management',
          'exposure_management',
          'exposure_and_execution_management',
        ],
      },
    },
    {
      path: 'min_position_size_usd',
      control_type: 'number',
      reload_policy: 'invocation_time',
      constraints: { minimum: 1 },
    },
    {
      path: 'rule_values',
      control_type: 'object-array',
      reload_policy: 'invocation_time',
      constraints: {},
      columns: [
        {
          path: 'key',
          control_type: 'string',
          reload_policy: 'invocation_time',
          constraints: {},
        },
        {
          path: 'value',
          control_type: 'number',
          reload_policy: 'invocation_time',
          constraints: {},
        },
      ],
    },
  ],
}

// Representative regime schema fixture.
const REGIME_SCHEMA: FormSchema = {
  slug: 'regimes/normal',
  filename: 'regimes/normal.yaml',
  fields: [
    {
      path: 'multipliers',
      control_type: 'object-array',
      reload_policy: 'invocation_time',
      constraints: {},
      columns: [
        {
          path: 'key',
          control_type: 'string',
          reload_policy: 'invocation_time',
          constraints: {},
        },
        {
          path: 'value',
          control_type: 'number',
          reload_policy: 'invocation_time',
          constraints: {},
        },
      ],
    },
    {
      path: 'transition.tighten_on_entry',
      control_type: 'enum',
      reload_policy: 'invocation_time',
      constraints: { enum_choices: ['immediate'] },
    },
  ],
}

// Build a fetch mock that returns the schema for ``/schema/`` URLs and
// the content (with seeded values) for ``/api/views/config/{slug}``
// URLs.  Mirrors the dual fetch the ConfigEditorPage scaffold issues.
function makeEditorFetchMock(
  schema: FormSchema,
  contentValues: Record<string, unknown> = {},
): ReturnType<typeof vi.fn> {
  return vi.fn().mockImplementation((url: string) => {
    if (typeof url === 'string' && url.includes('/schema/')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(schema),
      })
    }
    if (typeof url === 'string' && url.includes('family=')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ family: 'profiles', slugs: [] }),
      })
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          slug: schema.slug,
          filename: schema.filename,
          yaml: '',
          values: contentValues,
        }),
    })
  })
}

// Returns a fetch mock that serves schema + content then a successful
// save response, then re-serves schema + content on the post-save
// refetch.
function makeProfileSaveFetchMock(): ReturnType<typeof vi.fn> {
  return vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'PUT') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            slug: 'profiles/small',
            filename: 'profiles/small.yaml',
            deploy_time_fields_changed: false,
          }),
      })
    }
    if (typeof url === 'string' && url.includes('/schema/')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(PROFILE_SCHEMA),
      })
    }
    if (typeof url === 'string' && url.includes('family=')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ family: 'profiles', slugs: [] }),
      })
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          slug: 'profiles/small',
          filename: 'profiles/small.yaml',
          yaml: '',
          values: {},
        }),
    })
  })
}

// Stub file-list + schema query + content query in sequence.
function stubFileListThenSchema(
  slugs: readonly string[],
  family: string,
  schema: FormSchema,
): void {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (typeof url === 'string' && url.includes('family=')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ family, slugs }),
      })
    }
    if (typeof url === 'string' && url.includes('/schema/')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(schema),
      })
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () =>
        Promise.resolve({
          slug: schema.slug,
          filename: schema.filename,
          yaml: '',
          values: {},
        }),
    })
  })
  vi.stubGlobal('fetch', fetchMock)
}

// ── FilePicker ────────────────────────────────────────────────────────────────

describe('FilePicker', () => {
  it('renders a nav link per slug', () => {
    render(
      <FilePicker
        family="profiles"
        slugs={['profiles/small', 'profiles/medium']}
        activeSlug="profiles/small"
        basePath="/config/profiles"
      />,
    )
    expect(screen.getByText('small')).toBeInTheDocument()
    expect(screen.getByText('medium')).toBeInTheDocument()
  })

  it('highlights the active slug', () => {
    render(
      <FilePicker
        family="profiles"
        slugs={['profiles/small', 'profiles/medium']}
        activeSlug="profiles/small"
        basePath="/config/profiles"
      />,
    )
    const smallLink = screen.getByText('small').closest('a')
    expect(smallLink).toHaveAttribute('href', '/config/profiles/small')
  })

  it('renders empty nav when slugs list is empty', () => {
    render(
      <FilePicker
        family="profiles"
        slugs={[]}
        activeSlug="profiles/small"
        basePath="/config/profiles"
      />,
    )
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})

// ── ProfileEditorPage ─────────────────────────────────────────────────────────

beforeEach(() => {
  vi.unstubAllGlobals()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ProfileEditorPage', () => {
  it('renders loading state initially', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockReturnValue(
        new Promise((_resolve) => {
          /* never resolves */
        }),
      ),
    )
    render(wrap(<ProfileEditorPage profileName="small" />))
    expect(await screen.findByText(/loading profiles\/small\.yaml/i)).toBeInTheDocument()
  })

  it('renders the profile form after schema loads', async () => {
    vi.stubGlobal('fetch', makeEditorFetchMock(PROFILE_SCHEMA))
    render(wrap(<ProfileEditorPage profileName="small" />))
    expect(await screen.findByText(/profile: small/i)).toBeInTheDocument()
  })

  it('seeds the form with the values returned by GET /{slug}', async () => {
    // Regression guard: pre-fix the editor pages opened with an empty
    // ``{}`` and a save would overwrite the on-disk YAML with ``{}``.
    vi.stubGlobal(
      'fetch',
      makeEditorFetchMock(PROFILE_SCHEMA, {
        risk_priority: 'signal_quality',
        min_position_size_usd: 1234,
      }),
    )
    render(wrap(<ProfileEditorPage profileName="small" />))
    await screen.findByText(/profile: small/i)
    expect(await screen.findByDisplayValue(1234)).toBeInTheDocument()
  })

  it('renders ReloadPolicyBadge for each field', async () => {
    vi.stubGlobal('fetch', makeEditorFetchMock(PROFILE_SCHEMA))
    render(wrap(<ProfileEditorPage profileName="small" />))
    // All three schema fields carry invocation_time.
    const badges = await screen.findAllByText(/invocation/i)
    expect(badges.length).toBeGreaterThanOrEqual(PROFILE_SCHEMA.fields.length)
  })

  it('dispatches rule_values to ObjectArrayTableEditor', async () => {
    vi.stubGlobal('fetch', makeEditorFetchMock(PROFILE_SCHEMA))
    render(wrap(<ProfileEditorPage profileName="small" />))
    // ObjectArrayTableEditor renders an "Add row" button.
    expect(await screen.findByRole('button', { name: /add row/i })).toBeInTheDocument()
  })

  it('shows transition-discipline banner after successful save', async () => {
    const user = userEvent.setup()
    vi.stubGlobal('fetch', makeProfileSaveFetchMock())

    render(wrap(<ProfileEditorPage profileName="small" />))
    await screen.findByText(/profile: small/i)
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(await screen.findByText(/next-invocation reload/i)).toBeInTheDocument()
  })
})

// ── RegimeEditorPage ──────────────────────────────────────────────────────────

describe('RegimeEditorPage', () => {
  it('renders the regime form after schema loads', async () => {
    vi.stubGlobal('fetch', makeEditorFetchMock(REGIME_SCHEMA))
    render(wrap(<RegimeEditorPage regimeName="normal" />))
    expect(await screen.findByText(/regime: normal/i)).toBeInTheDocument()
  })

  it('dispatches multipliers to ObjectArrayTableEditor', async () => {
    vi.stubGlobal('fetch', makeEditorFetchMock(REGIME_SCHEMA))
    render(wrap(<RegimeEditorPage regimeName="normal" />))
    // ObjectArrayTableEditor renders an "Add row" button.
    expect(await screen.findByRole('button', { name: /add row/i })).toBeInTheDocument()
  })

  it('renders file-picker sidebar with regime slugs', async () => {
    stubFileListThenSchema(['regimes/normal', 'regimes/crisis'], 'regimes', REGIME_SCHEMA)
    render(wrap(<RegimeEditorPage regimeName="normal" />))
    await screen.findByText(/regime: normal/i)
    // File-picker renders after the list query resolves.
    expect(await screen.findByText('crisis')).toBeInTheDocument()
  })
})
