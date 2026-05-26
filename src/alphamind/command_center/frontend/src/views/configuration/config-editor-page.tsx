// ConfigEditorPage — shared scaffold for the per-file editor routes (ALP-683).
//
// Composes the 05i framework primitives:
//
//  * Fetches GET /api/views/config/{slug} to seed initial form state.
//  * Fetches GET /api/views/config/schema/{slug} to drive FormComposer.
//  * Tracks form state + computes the YAML body via js-yaml at save time.
//  * Renders SaveAction; on alerts.yaml save success, surfaces an
//    engine-reload toast.
//
// Per-file routes (alerts, security, command-center, digest) pass their
// slug + human title; the scaffold owns the load + edit + save dance.

import { useEffect, useMemo, useState } from 'react'

import * as yaml from 'js-yaml'

import { api, ApiError } from '@/api/client'
import { FormComposer } from '@/views/configuration/framework/form-composer'
import { SaveAction } from '@/views/configuration/framework/save-action'
import type { FormSchema } from '@/views/configuration/framework/types'

type ConfigContentResponse = {
  slug: string
  filename: string
  yaml: string
  values: Record<string, unknown>
}

type LoadStatus =
  | { kind: 'loading' }
  | { kind: 'ready'; schema: FormSchema; initial: ConfigContentResponse }
  | { kind: 'error'; message: string }

type ConfigEditorPageProps = {
  configFileSlug: string
  title: string
  // Hook the page surfaces on save success — typically a toast in the
  // shared layout. Defaults to a no-op so unit tests don't need to wire
  // a stub.
  onSaved?: (result: { deployTimeFieldsChanged: boolean }) => void
}

async function loadEditorState(slug: string): Promise<LoadStatus> {
  try {
    const [schema, initial] = await Promise.all([
      api.get<FormSchema>(`/api/views/config/schema/${slug}`),
      api.get<ConfigContentResponse>(`/api/views/config/${slug}`),
    ])
    return { kind: 'ready', schema, initial }
  } catch (error) {
    const message = error instanceof ApiError ? `HTTP ${String(error.status)}` : 'Network error'
    return { kind: 'error', message }
  }
}

function hasDeployTimeReloadPolicy(schema: FormSchema): boolean {
  return schema.fields.some((field) => field.reload_policy === 'deploy_time')
}

function ConfigEditorReadyView({
  configFileSlug,
  title,
  schema,
  initial,
  onSaved,
}: {
  configFileSlug: string
  title: string
  schema: FormSchema
  initial: ConfigContentResponse
  onSaved: (result: { deployTimeFieldsChanged: boolean }) => void
}): React.JSX.Element {
  const [value, setValue] = useState<Record<string, unknown>>(initial.values)
  const yamlBody = useMemo(() => yaml.dump(value, { noRefs: true }), [value])
  const deployTimeFieldsTouched = useMemo(
    () => hasDeployTimeReloadPolicy(schema) && yamlBody !== initial.yaml,
    [schema, yamlBody, initial.yaml],
  )
  return (
    <section className="container mx-auto flex flex-col gap-6 px-4 py-6">
      <header>
        <h1 className="text-xl font-semibold">{title}</h1>
        <p className="text-muted-foreground text-sm">{initial.filename}</p>
      </header>
      <FormComposer schema={schema} value={value} onChange={setValue} />
      <SaveAction
        configFileSlug={configFileSlug}
        yamlBody={yamlBody}
        hasClientValidationErrors={false}
        deployTimeFieldsTouched={deployTimeFieldsTouched}
        onSaved={onSaved}
      />
    </section>
  )
}

function _noopSaved(_result: { deployTimeFieldsChanged: boolean }): void {
  // Default ``onSaved`` for routes that don't need post-save side effects.
  // Pulled out of the default-arg position so the lint rule's ban on
  // empty arrow expressions doesn't flag a no-op page wiring.
}

export function ConfigEditorPage({
  configFileSlug,
  title,
  onSaved = _noopSaved,
}: ConfigEditorPageProps): React.JSX.Element {
  const [status, setStatus] = useState<LoadStatus>({ kind: 'loading' })
  useEffect(() => {
    let cancelled = false
    const run = async (): Promise<void> => {
      const next = await loadEditorState(configFileSlug)
      if (!cancelled) {
        setStatus(next)
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [configFileSlug])
  if (status.kind === 'loading') {
    return (
      <section className="container mx-auto flex flex-col gap-2 px-4 py-6">
        <p className="text-muted-foreground text-sm">Loading {configFileSlug}.yaml…</p>
      </section>
    )
  }
  if (status.kind === 'error') {
    return (
      <section className="container mx-auto flex flex-col gap-2 px-4 py-6">
        <p className="text-destructive text-sm">
          Failed to load {configFileSlug}.yaml ({status.message}).
        </p>
      </section>
    )
  }
  return (
    <ConfigEditorReadyView
      configFileSlug={configFileSlug}
      title={title}
      schema={status.schema}
      initial={status.initial}
      onSaved={onSaved}
    />
  )
}
