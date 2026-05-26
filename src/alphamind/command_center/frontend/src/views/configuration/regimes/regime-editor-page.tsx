// Regime editor page — /config/regimes/$regimeName (ALP-682).
//
// Composes the framework's FormComposer + SaveAction for a single
// regime YAML file. The file-picker sidebar lists all registered
// regime slugs from GET /api/views/config/files?family=regimes.
//
// After a successful save, an invocation-time banner reminds the
// operator that regime changes take effect at the next invocation.

import { useState } from 'react'

import { useConfigFileList, useConfigSchema } from '@/api/configuration'
import dump from '@/lib/yaml-dump'
import { FormComposer } from '@/views/configuration/framework/form-composer'
import { SaveAction } from '@/views/configuration/framework/save-action'
import type { FormSchema } from '@/views/configuration/framework/types'
import { ValidationBanner } from '@/views/configuration/framework/validation-banner'
import { FilePicker } from '@/views/configuration/shared/file-picker'

type RegimeEditorPageProps = {
  regimeName: string
}

function InvocationTimeBanner({ visible }: { visible: boolean }): React.JSX.Element | null {
  if (!visible) {
    return null
  }
  return (
    <div
      role="status"
      className="bg-muted text-foreground mb-4 rounded-md border px-4 py-3 text-sm"
    >
      <strong>Next-invocation reload.</strong> Regime multiplier changes take effect when the system
      triggers the next scheduled invocation. Loosen-on-exit transitions follow the profile&apos;s{' '}
      <code>linear_over_invocations_3</code> schedule once the VIX band clears.
    </div>
  )
}

type EditorBodyProps = {
  schema: FormSchema
  regimeName: string
}

function EditorBody({ schema, regimeName }: EditorBodyProps): React.JSX.Element {
  const [value, setValue] = useState<Record<string, unknown>>({})
  const [savedOk, setSavedOk] = useState(false)

  const yamlBody = dump(value)

  return (
    <div className="flex flex-col gap-6">
      <InvocationTimeBanner visible={savedOk} />
      <ValidationBanner report={{ parse: [], cross_reference: [], semantic: [] }} />
      <FormComposer schema={schema} value={value} onChange={setValue} />
      <SaveAction
        configFileSlug={`regimes/${regimeName}`}
        yamlBody={yamlBody}
        hasClientValidationErrors={false}
        deployTimeFieldsTouched={false}
        onSaved={() => {
          setSavedOk(true)
        }}
      />
    </div>
  )
}

export function RegimeEditorPage({ regimeName }: RegimeEditorPageProps): React.JSX.Element {
  const listResult = useConfigFileList('regimes')
  const schemaResult = useConfigSchema(`regimes/${regimeName}`)

  const sidebar = (
    <FilePicker
      family="regimes"
      slugs={listResult.data?.slugs ?? []}
      activeSlug={`regimes/${regimeName}`}
      basePath="/config/regimes"
    />
  )

  if (schemaResult.isPending) {
    return (
      <div className="flex gap-6">
        <div className="w-48 shrink-0">{sidebar}</div>
        <div className="flex-1">
          <span className="text-muted-foreground">Loading schema…</span>
        </div>
      </div>
    )
  }

  if (schemaResult.isError) {
    return (
      <div className="flex gap-6">
        <div className="w-48 shrink-0">{sidebar}</div>
        <div className="flex-1">
          <span className="text-destructive">Failed to load regime schema.</span>
        </div>
      </div>
    )
  }

  return (
    <div className="flex gap-6">
      <div className="w-48 shrink-0">{sidebar}</div>
      <div className="flex-1">
        <h1 className="mb-4 text-xl font-semibold">Regime: {regimeName}</h1>
        <EditorBody schema={schemaResult.data} regimeName={regimeName} />
      </div>
    </div>
  )
}
