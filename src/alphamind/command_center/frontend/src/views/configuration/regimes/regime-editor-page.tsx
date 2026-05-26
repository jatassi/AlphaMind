// Regime editor page — /config/regimes/$regimeName (ALP-682).
//
// Delegates to the shared :func:`ConfigEditorPage` scaffold so the
// load-then-seed-then-refetch dance and save flow stay in lockstep with
// the alerts/security/digest editors.  Provides the per-family extras:
//
//  * Left-rail :func:`FilePicker` listing every regime slug.
//  * Invocation-time banner reminding the operator that regime
//    multiplier changes take effect at the next scheduled invocation
//    and that loosen-on-exit transitions follow the profile's
//    ``linear_over_invocations_3`` schedule.

import { useState } from 'react'

import { useConfigFileList } from '@/api/configuration'
import { ConfigEditorPage } from '@/views/configuration/config-editor-page'
import { FilePicker } from '@/views/configuration/shared/file-picker'

type RegimeEditorPageProps = {
  regimeName: string
}

function InvocationTimeBanner(): React.JSX.Element {
  return (
    <div role="status" className="bg-muted text-foreground rounded-md border px-4 py-3 text-sm">
      <strong>Next-invocation reload.</strong> Regime multiplier changes take effect when the system
      triggers the next scheduled invocation. Loosen-on-exit transitions follow the profile&apos;s{' '}
      <code>linear_over_invocations_3</code> schedule once the VIX band clears.
    </div>
  )
}

export function RegimeEditorPage({ regimeName }: RegimeEditorPageProps): React.JSX.Element {
  const listResult = useConfigFileList('regimes')
  const [savedOnce, setSavedOnce] = useState(false)

  const sidebar = (
    <FilePicker
      family="regimes"
      slugs={listResult.data?.slugs ?? []}
      activeSlug={`regimes/${regimeName}`}
      basePath="/config/regimes"
    />
  )

  return (
    <ConfigEditorPage
      configFileSlug={`regimes/${regimeName}`}
      title={`Regime: ${regimeName}`}
      sidebar={sidebar}
      bannerAboveForm={savedOnce ? <InvocationTimeBanner /> : null}
      onSaved={() => {
        setSavedOnce(true)
      }}
    />
  )
}
