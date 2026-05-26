// Regime index landing page — /config/regimes (Wave-6 #13).
//
// Same shape as :func:`ProfileIndexPage`: the Configuration dropdown
// links operators here so they can pick a regime slug before editing.

import { useConfigFileList } from '@/api/configuration'
import { FilePicker } from '@/views/configuration/shared/file-picker'

function IndexStatus({
  isPending,
  isError,
}: {
  isPending: boolean
  isError: boolean
}): React.JSX.Element {
  if (isPending) {
    return <p className="text-muted-foreground text-sm">Loading regime list…</p>
  }
  if (isError) {
    return <p className="text-destructive text-sm">Failed to load regime list.</p>
  }
  return (
    <p className="text-muted-foreground text-sm">Select a regime from the left to edit its YAML.</p>
  )
}

export function RegimeIndexPage(): React.JSX.Element {
  const { data, isPending, isError } = useConfigFileList('regimes')
  return (
    <div className="container mx-auto flex gap-6 px-4 py-6">
      <div className="w-48 shrink-0">
        <FilePicker
          family="regimes"
          slugs={data?.slugs ?? []}
          activeSlug=""
          basePath="/config/regimes"
        />
      </div>
      <div className="flex-1">
        <h1 className="mb-4 text-xl font-semibold">Regimes</h1>
        <IndexStatus isPending={isPending} isError={isError} />
      </div>
    </div>
  )
}
