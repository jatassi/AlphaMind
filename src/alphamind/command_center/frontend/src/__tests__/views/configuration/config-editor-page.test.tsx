// Vitest coverage for the per-file config editor pages (ALP-683).

import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AlertsConfigPage } from '@/views/configuration/alerts-page'
import { ConfigEditorPage } from '@/views/configuration/config-editor-page'

function _buildStubResponse(url: string, responses: Record<string, unknown>): Response {
  const body = responses[url]
  if (body === undefined) {
    return {
      ok: false,
      status: 404,
      json: () => Promise.resolve({ detail: 'not stubbed' }),
    } as unknown as Response
  }
  return {
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  } as unknown as Response
}

function _stubLoadResponses(responses: Record<string, unknown>): ReturnType<typeof vi.fn> {
  // Map each requested URL to a JSON payload. The fetch mock 200s the
  // first matching key + 404s anything unmatched (so a missing stub
  // surfaces in the test rather than silently passing).
  const fetchMock = vi.fn((url: string) => Promise.resolve(_buildStubResponse(url, responses)))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

const SECURITY_SCHEMA = {
  slug: 'security',
  filename: 'security.yaml',
  fields: [
    {
      path: 'session.duration_hours',
      control_type: 'number',
      reload_policy: 'invocation_time',
      constraints: { minimum: 1 },
    },
    {
      path: 'webauthn.relying_party_id',
      control_type: 'string',
      reload_policy: 'deploy_time',
      constraints: {},
    },
  ],
}

const SECURITY_CONTENT = {
  slug: 'security',
  filename: 'security.yaml',
  yaml: 'session:\n  duration_hours: 12\nwebauthn:\n  relying_party_id: localhost\n',
  values: {
    session: { duration_hours: 12 },
    webauthn: { relying_party_id: 'localhost' },
  },
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function _expectSecurityYamlVisible(): void {
  expect(screen.getByText(/security\.yaml/i)).toBeInTheDocument()
}

function _expectLoadErrorVisible(): void {
  expect(screen.getByText(/Failed to load security\.yaml/i)).toBeInTheDocument()
}

describe('ConfigEditorPage', () => {
  it('renders a loading indicator before fetch resolves', () => {
    _stubLoadResponses({})
    render(<ConfigEditorPage configFileSlug="security" title="Security" />)
    expect(screen.getByText(/Loading security\.yaml/i)).toBeInTheDocument()
  })

  it('renders the form once schema + values load', async () => {
    _stubLoadResponses({
      '/api/views/config/schema/security': SECURITY_SCHEMA,
      '/api/views/config/security': SECURITY_CONTENT,
    })
    render(<ConfigEditorPage configFileSlug="security" title="Security" />)
    await waitFor(_expectSecurityYamlVisible)
    expect(screen.getByLabelText(/session\.duration_hours/i)).toHaveValue(12)
    expect(screen.getByLabelText(/webauthn\.relying_party_id/i)).toHaveValue('localhost')
  })

  it('surfaces a deploy-time badge on the WebAuthn RP id field', async () => {
    _stubLoadResponses({
      '/api/views/config/schema/security': SECURITY_SCHEMA,
      '/api/views/config/security': SECURITY_CONTENT,
    })
    render(<ConfigEditorPage configFileSlug="security" title="Security" />)
    await waitFor(_expectSecurityYamlVisible)
    // One DEPLOY_TIME field in the fixture; the badge label includes "deploy".
    const badges = screen.getAllByText(/deploy/i)
    expect(badges.length).toBeGreaterThanOrEqual(1)
  })

  it('surfaces a load error when the API rejects', async () => {
    _stubLoadResponses({})
    render(<ConfigEditorPage configFileSlug="security" title="Security" />)
    await waitFor(_expectLoadErrorVisible)
  })
})

const ALERTS_SCHEMA = {
  slug: 'alerts',
  filename: 'alerts.yaml',
  fields: [
    {
      path: 'rules',
      control_type: 'object-array',
      reload_policy: 'invocation_time',
      constraints: {},
      columns: [
        {
          path: 'name',
          control_type: 'string',
          reload_policy: 'invocation_time',
          constraints: {},
        },
        {
          path: 'severity',
          control_type: 'string',
          reload_policy: 'invocation_time',
          constraints: {},
        },
        {
          path: 'debounce_minutes',
          control_type: 'number',
          reload_policy: 'invocation_time',
          constraints: { minimum: 1 },
        },
      ],
    },
    {
      path: 'channels.discord.webhook_url_env',
      control_type: 'string',
      reload_policy: 'invocation_time',
      constraints: {},
    },
  ],
}

const ALERTS_CONTENT = {
  slug: 'alerts',
  filename: 'alerts.yaml',
  yaml:
    'rules:\n' +
    '  - name: pipeline_aborted\n' +
    '    severity: critical\n' +
    '    debounce_minutes: 15\n' +
    '    channels: [in_app]\n' +
    'channels:\n' +
    '  discord:\n' +
    '    webhook_url_env: ALPHAMIND_DISCORD_WEBHOOK\n',
  values: {
    rules: [
      {
        name: 'pipeline_aborted',
        severity: 'critical',
        debounce_minutes: 15,
        channels: ['in_app'],
      },
    ],
    channels: { discord: { webhook_url_env: 'ALPHAMIND_DISCORD_WEBHOOK' } },
  },
}

function _expectAlertsYamlVisible(): void {
  expect(screen.getByText(/alerts\.yaml/i)).toBeInTheDocument()
}

describe('AlertsConfigPage', () => {
  it('renders the alerts table editor with the seeded rule row', async () => {
    _stubLoadResponses({
      '/api/views/config/schema/alerts': ALERTS_SCHEMA,
      '/api/views/config/alerts': ALERTS_CONTENT,
    })
    render(<AlertsConfigPage />)
    await waitFor(_expectAlertsYamlVisible)
    // Rule row's cells render via the ObjectArrayTableEditor.
    expect(screen.getByDisplayValue('pipeline_aborted')).toBeInTheDocument()
    expect(screen.getByDisplayValue('critical')).toBeInTheDocument()
  })
})
