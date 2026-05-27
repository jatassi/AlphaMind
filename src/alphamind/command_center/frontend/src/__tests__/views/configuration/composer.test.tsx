// Vitest tests for the config editor framework composer (ALP-679):
// FormComposer (schema-driven render), ValidationBanner (layered errors),
// ReloadPolicyBadge (per-field badge), SaveAction (PUT gate + toast).

import { useState } from 'react'

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FormComposer } from '@/views/configuration/framework/form-composer'
import {
  ReloadPolicyBadge,
  type ReloadPolicyValue,
} from '@/views/configuration/framework/reload-policy-badge'
import { SaveAction } from '@/views/configuration/framework/save-action'
import type { FormSchema } from '@/views/configuration/framework/types'
import { ValidationBanner } from '@/views/configuration/framework/validation-banner'

// ── ReloadPolicyBadge ─────────────────────────────────────────────────────────

describe('ReloadPolicyBadge', () => {
  const cases: { policy: ReloadPolicyValue; expected: RegExp }[] = [
    { policy: 'invocation_time', expected: /invocation/i },
    { policy: 'deploy_time', expected: /deploy/i },
  ]
  it.each(cases)('renders the $policy badge', ({ policy, expected }) => {
    render(<ReloadPolicyBadge policy={policy} />)
    expect(screen.getByText(expected)).toBeInTheDocument()
  })
})

// ── ValidationBanner ──────────────────────────────────────────────────────────

describe('ValidationBanner', () => {
  it('renders nothing when every layer is empty', () => {
    const { container } = render(
      <ValidationBanner report={{ parse: [], cross_reference: [], semantic: [] }} />,
    )
    expect(container.textContent).toBe('')
  })

  it('renders parse layer errors', () => {
    render(
      <ValidationBanner
        report={{
          parse: [{ path: 'bind.port', message: 'out of range' }],
          cross_reference: [],
          semantic: [],
        }}
      />,
    )
    expect(screen.getByText(/out of range/i)).toBeInTheDocument()
    expect(screen.getByText(/bind\.port/i)).toBeInTheDocument()
  })

  it('renders cross-reference layer errors', () => {
    render(
      <ValidationBanner
        report={{
          parse: [],
          cross_reference: [{ path: '', message: 'active_profile not found' }],
          semantic: [],
        }}
      />,
    )
    expect(screen.getByText(/active_profile not found/i)).toBeInTheDocument()
    expect(screen.getByText(/cross-reference/i)).toBeInTheDocument()
  })

  it('renders semantic layer errors', () => {
    render(
      <ValidationBanner
        report={{
          parse: [],
          cross_reference: [],
          semantic: [{ path: '', message: 'invariant violated' }],
        }}
      />,
    )
    expect(screen.getByText(/invariant violated/i)).toBeInTheDocument()
    expect(screen.getByText(/semantic/i)).toBeInTheDocument()
  })
})

// ── FormComposer ──────────────────────────────────────────────────────────────

const FIXTURE_SCHEMA: FormSchema = {
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
      path: 'session.cookie_name',
      control_type: 'string',
      reload_policy: 'invocation_time',
      constraints: { minLength: 1 },
    },
    {
      path: 'webauthn.use_resident_keys',
      control_type: 'boolean',
      reload_policy: 'invocation_time',
      constraints: {},
    },
  ],
}

function ControlledFormComposer({
  initial,
  onChange,
}: {
  initial: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
}): React.JSX.Element {
  const [value, setValue] = useState(initial)
  return (
    <FormComposer
      schema={FIXTURE_SCHEMA}
      value={value}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

describe('FormComposer', () => {
  it('renders one control per schema field', () => {
    render(
      <FormComposer
        schema={FIXTURE_SCHEMA}
        value={{
          session: { duration_hours: 12, cookie_name: 'cc_session' },
          webauthn: { use_resident_keys: false },
        }}
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByLabelText(/session\.duration_hours/i)).toHaveValue(12)
    expect(screen.getByLabelText(/session\.cookie_name/i)).toHaveValue('cc_session')
    expect(screen.getByLabelText(/webauthn\.use_resident_keys/i)).not.toBeChecked()
  })

  it('renders a reload-policy badge for each field', () => {
    render(
      <FormComposer
        schema={FIXTURE_SCHEMA}
        value={{
          session: { duration_hours: 12, cookie_name: 'cc_session' },
          webauthn: { use_resident_keys: false },
        }}
        onChange={vi.fn()}
      />,
    )
    const badges = screen.getAllByText(/invocation/i)
    expect(badges.length).toBe(FIXTURE_SCHEMA.fields.length)
  })

  it('dispatches a value change with the nested path written', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ControlledFormComposer
        initial={{
          session: { duration_hours: 12, cookie_name: 'cc_session' },
          webauthn: { use_resident_keys: false },
        }}
        onChange={onChange}
      />,
    )
    await user.click(screen.getByLabelText(/webauthn\.use_resident_keys/i))
    const last = onChange.mock.calls.at(-1)?.[0] as {
      webauthn: { use_resident_keys: boolean }
    }
    expect(last.webauthn.use_resident_keys).toBe(true)
  })
})

// ── SaveAction ────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.unstubAllGlobals()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SaveAction rendering', () => {
  it('renders a Save button', () => {
    render(
      <SaveAction
        configFileSlug="security"
        yamlBody="x: 1\n"
        hasClientValidationErrors={false}
        deployTimeFieldsTouched={false}
        onSaved={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: /save/i })).toBeInTheDocument()
  })

  it('disables Save when client validation has errors', () => {
    render(
      <SaveAction
        configFileSlug="security"
        yamlBody="x: 1\n"
        hasClientValidationErrors
        deployTimeFieldsTouched={false}
        onSaved={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: /save/i })).toBeDisabled()
  })
})

function _stubSuccessfulPut(): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () =>
      Promise.resolve({
        slug: 'security',
        filename: 'security.yaml',
        deploy_time_fields_changed: false,
      }),
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('SaveAction PUT request', () => {
  it('PUTs to /api/views/config/{slug} on click', async () => {
    const user = userEvent.setup()
    const fetchMock = _stubSuccessfulPut()
    render(
      <SaveAction
        configFileSlug="security"
        yamlBody='x: "y"\n'
        hasClientValidationErrors={false}
        deployTimeFieldsTouched={false}
        onSaved={vi.fn()}
      />,
    )
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(fetchMock).toHaveBeenCalled()
    const [url, init] = fetchMock.mock.calls[0] as [string, { method?: string; body?: string }]
    expect(url).toBe('/api/views/config/security')
    expect(init.method).toBe('PUT')
    // JSON-encoded body wraps the YAML in the `yaml` key — round-trip
    // through JSON.parse to confirm the YAML body landed unchanged. The
    // test fixture's body uses backslash-n (literal, not a newline).
    expect(JSON.parse(init.body ?? '{}')).toEqual({ yaml: String.raw`x: "y"\n` })
  })

  it('injects the X-CSRF-Token header from the cc_csrf cookie', async () => {
    // Regression for the #1+#2 coupling identified in the Wave-5
    // review: the previous implementation used raw ``fetch`` with no
    // CSRF header; once the backend's PUT gate landed, every Save
    // click 403'd. Routing through ``api.put`` auto-injects the
    // ``X-CSRF-Token`` header from the cookie via ``buildHeaders``.
    const user = userEvent.setup()
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: () => 'cc_csrf=abc123',
    })
    const fetchMock = _stubSuccessfulPut()
    render(
      <SaveAction
        configFileSlug="security"
        yamlBody="x: 1\n"
        hasClientValidationErrors={false}
        deployTimeFieldsTouched={false}
        onSaved={vi.fn()}
      />,
    )
    await user.click(screen.getByRole('button', { name: /save/i }))
    const [, init] = fetchMock.mock.calls[0] as [string, { headers?: Headers }]
    expect(init.headers?.get('X-CSRF-Token')).toBe('abc123')
  })
})

describe('SaveAction response surfacing', () => {
  it('surfaces a deploy-time reminder when fields touched and save succeeds', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve({
            slug: 'security',
            filename: 'security.yaml',
            deploy_time_fields_changed: true,
          }),
      }),
    )
    render(
      <SaveAction
        configFileSlug="security"
        yamlBody="x: 1\n"
        hasClientValidationErrors={false}
        deployTimeFieldsTouched
        onSaved={vi.fn()}
      />,
    )
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(await screen.findByText(/restart/i)).toBeInTheDocument()
  })
})

describe('SaveAction rejection surfacing', () => {
  it('surfaces a validation rejection from the backend', async () => {
    const user = userEvent.setup()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: () =>
          Promise.resolve({
            detail: {
              parse: [{ path: 'bind.port', message: 'invalid' }],
              cross_reference: [],
              semantic: [],
            },
          }),
      }),
    )
    render(
      <SaveAction
        configFileSlug="security"
        yamlBody="x: 1\n"
        hasClientValidationErrors={false}
        deployTimeFieldsTouched={false}
        onSaved={vi.fn()}
      />,
    )
    await user.click(screen.getByRole('button', { name: /save/i }))
    expect(await screen.findByText(/rejected/i)).toBeInTheDocument()
  })
})
