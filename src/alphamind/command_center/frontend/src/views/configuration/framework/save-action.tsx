// SaveAction — gated PUT button for the config editor framework (ALP-679).
//
// Disabled when the client-side parse layer surfaces any errors (the
// FormComposer pre-validates against the schema's constraints). PUTs to
// /api/views/config/{slug} with the proposed YAML body via the shared
// ``api`` wrapper so the ``X-CSRF-Token`` header is auto-injected from
// the ``cc_csrf`` cookie (per the project's mutating-verb contract).
//
// Renders a toast on success (with a restart reminder when any
// deploy-time field was touched) or a layered envelope when the
// backend rejects.

import { useState } from 'react'

import { api, ApiError } from '@/api/client'
import { cn } from '@/lib/utils'

import type { ValidationReport } from './types'

type SaveActionProps = {
  configFileSlug: string
  yamlBody: string
  hasClientValidationErrors: boolean
  deployTimeFieldsTouched: boolean
  onSaved: (result: { deployTimeFieldsChanged: boolean }) => void
  className?: string
}

type SaveSuccessBody = { deploy_time_fields_changed: boolean }

type SaveStatus =
  | { kind: 'idle' }
  | { kind: 'success'; restartReminder: boolean }
  | { kind: 'error'; report: ValidationReport | null; httpStatus: number }

type SaveOutcome =
  | { ok: true; body: SaveSuccessBody }
  | { ok: false; status: number; body: unknown }

async function postUpdate(slug: string, yamlBody: string): Promise<SaveOutcome> {
  // The shared ``api.put`` wrapper auto-injects the ``X-CSRF-Token``
  // header (read from the ``cc_csrf`` cookie); without that header the
  // backend's ``csrf_required`` dependency 403s every Save click
  // (regression for the #1 + #2 coupling identified in the Wave-5
  // review).
  try {
    const body = await api.put<SaveSuccessBody>(`/api/views/config/${slug}`, {
      yaml: yamlBody,
    })
    return { ok: true, body }
  } catch (error) {
    if (error instanceof ApiError) {
      return { ok: false, status: error.status, body: error.detail }
    }
    throw error
  }
}

function extractReport(body: unknown): ValidationReport | null {
  if (
    typeof body !== 'object' ||
    body === null ||
    !('detail' in (body as Record<string, unknown>))
  ) {
    // ``ApiError.detail`` already carries the parsed response body
    // (without the surrounding ``{detail: ...}`` envelope), so accept
    // the bare ValidationReport shape too.
    if (typeof body === 'object' && body !== null && 'parse' in (body as Record<string, unknown>)) {
      return body as ValidationReport
    }
    return null
  }
  const detail = (body as { detail: unknown }).detail
  if (typeof detail !== 'object' || detail === null) {
    return null
  }
  // Trust the shape — the backend returns the ValidationReport envelope.
  return detail as ValidationReport
}

function StatusBanner({ status }: { status: SaveStatus }): React.JSX.Element | null {
  if (status.kind === 'idle') {
    return null
  }
  if (status.kind === 'success') {
    return (
      <div className="bg-muted text-foreground rounded-md px-3 py-2 text-sm">
        Saved.
        {status.restartReminder ? (
          <span className="ml-2 font-semibold">Restart required to apply changes.</span>
        ) : null}
      </div>
    )
  }
  return (
    <div className="bg-destructive/10 text-destructive rounded-md px-3 py-2 text-sm">
      Save rejected (HTTP {status.httpStatus.toString()}). Inline errors above.
    </div>
  )
}

export function SaveAction({
  configFileSlug,
  yamlBody,
  hasClientValidationErrors,
  deployTimeFieldsTouched,
  onSaved,
  className,
}: SaveActionProps): React.JSX.Element {
  const [status, setStatus] = useState<SaveStatus>({ kind: 'idle' })
  const [submitting, setSubmitting] = useState(false)

  const handleClick = async (): Promise<void> => {
    setSubmitting(true)
    try {
      const outcome = await postUpdate(configFileSlug, yamlBody)
      if (outcome.ok) {
        const deployChanged = outcome.body.deploy_time_fields_changed
        setStatus({
          kind: 'success',
          restartReminder: deployTimeFieldsTouched || deployChanged,
        })
        onSaved({ deployTimeFieldsChanged: deployChanged })
      } else {
        setStatus({
          kind: 'error',
          report: extractReport(outcome.body),
          httpStatus: outcome.status,
        })
      }
    } finally {
      setSubmitting(false)
    }
  }
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <button
        type="button"
        disabled={hasClientValidationErrors || submitting}
        onClick={() => {
          void handleClick()
        }}
        className="bg-primary text-primary-foreground hover:bg-primary/90 h-10 cursor-pointer rounded-md px-4 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50"
      >
        Save
      </button>
      <StatusBanner status={status} />
    </div>
  )
}
