// Profile editor page — /config/profiles/$profileName (ALP-682).
//
// Delegates to the shared :func:`ConfigEditorPage` scaffold so the
// load-then-seed-then-refetch dance, YAML serialization, and save flow
// stay in lockstep with the alerts/security/digest editors.  Provides
// the per-family extras:
//
//  * Left-rail :func:`FilePicker` listing every profile slug.
//  * Invocation-time banner (transition-discipline reminder) rendered
//    above the form, visible only after the operator saves at least
//    once — drawn from docs/design/06-risk-guardrails/rules-and-limits.md
//    § Transitioning between profiles.

import { useState } from 'react'

import { useConfigFileList } from '@/api/configuration'
import { ConfigEditorPage } from '@/views/configuration/config-editor-page'
import { FilePicker } from '@/views/configuration/shared/file-picker'

type ProfileEditorPageProps = {
  profileName: string
}

function InvocationTimeBanner(): React.JSX.Element {
  return (
    <div role="status" className="bg-muted text-foreground rounded-md border px-4 py-3 text-sm">
      <strong>Next-invocation reload.</strong> Profile changes take effect when the system triggers
      the next scheduled invocation — running invocations are not interrupted. To activate a profile
      change immediately, wait for the current invocation to complete and then re-trigger via the
      Live page.
    </div>
  )
}

export function ProfileEditorPage({ profileName }: ProfileEditorPageProps): React.JSX.Element {
  const listResult = useConfigFileList('profiles')
  const [savedOnce, setSavedOnce] = useState(false)

  const sidebar = (
    <FilePicker
      family="profiles"
      slugs={listResult.data?.slugs ?? []}
      activeSlug={`profiles/${profileName}`}
      basePath="/config/profiles"
    />
  )

  return (
    <ConfigEditorPage
      configFileSlug={`profiles/${profileName}`}
      title={`Profile: ${profileName}`}
      sidebar={sidebar}
      bannerAboveForm={savedOnce ? <InvocationTimeBanner /> : null}
      onSaved={() => {
        setSavedOnce(true)
      }}
    />
  )
}
