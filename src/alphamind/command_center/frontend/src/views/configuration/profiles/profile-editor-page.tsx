// Profile editor page — /config/profiles/$profileName (ALP-682).
//
// Composes the framework's FormComposer + SaveAction for a single
// profile YAML file. The file-picker sidebar lists all registered
// profile slugs from GET /api/views/config/files?family=profiles.
//
// After a successful save, an invocation-time banner reminds the
// operator that profile changes take effect at the next invocation
// (per docs/design/06-risk-guardrails/rules-and-limits.md §
// Transitioning between profiles).

import { useState } from 'react'

import { useConfigFileList, useConfigSchema } from '@/api/configuration'
import dump from '@/lib/yaml-dump'
import { FormComposer } from '@/views/configuration/framework/form-composer'
import { SaveAction } from '@/views/configuration/framework/save-action'
import type { FormSchema } from '@/views/configuration/framework/types'
import { ValidationBanner } from '@/views/configuration/framework/validation-banner'
import { FilePicker } from '@/views/configuration/shared/file-picker'

type ProfileEditorPageProps = {
  profileName: string
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
      <strong>Next-invocation reload.</strong> Profile changes take effect when the system triggers
      the next scheduled invocation — running invocations are not interrupted. To activate a profile
      change immediately, wait for the current invocation to complete and then re-trigger via the
      Live page.
    </div>
  )
}

type EditorBodyProps = {
  schema: FormSchema
  profileName: string
}

function EditorBody({ schema, profileName }: EditorBodyProps): React.JSX.Element {
  const [value, setValue] = useState<Record<string, unknown>>({})
  const [savedOk, setSavedOk] = useState(false)

  const yamlBody = dump(value)

  return (
    <div className="flex flex-col gap-6">
      <InvocationTimeBanner visible={savedOk} />
      <ValidationBanner report={{ parse: [], cross_reference: [], semantic: [] }} />
      <FormComposer schema={schema} value={value} onChange={setValue} />
      <SaveAction
        configFileSlug={`profiles/${profileName}`}
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

export function ProfileEditorPage({ profileName }: ProfileEditorPageProps): React.JSX.Element {
  const listResult = useConfigFileList('profiles')
  const schemaResult = useConfigSchema(`profiles/${profileName}`)

  const sidebar = (
    <FilePicker
      family="profiles"
      slugs={listResult.data?.slugs ?? []}
      activeSlug={`profiles/${profileName}`}
      basePath="/config/profiles"
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
          <span className="text-destructive">Failed to load profile schema.</span>
        </div>
      </div>
    )
  }

  return (
    <div className="flex gap-6">
      <div className="w-48 shrink-0">{sidebar}</div>
      <div className="flex-1">
        <h1 className="mb-4 text-xl font-semibold">Profile: {profileName}</h1>
        <EditorBody schema={schemaResult.data} profileName={profileName} />
      </div>
    </div>
  )
}
