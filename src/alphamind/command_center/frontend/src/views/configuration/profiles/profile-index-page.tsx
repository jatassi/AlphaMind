// Profile index landing page — /config/profiles (Wave-6 #13).
//
// The Configuration dropdown links operators here so they can pick a
// profile slug before editing. Rendering only the FilePicker keeps
// the operator-flow consistent with the per-slug editor (left-rail
// FilePicker + main content panel) — picking a slug navigates them
// straight to ``/config/profiles/<slug>``.

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
    return <p className="text-muted-foreground text-sm">Loading profile list…</p>
  }
  if (isError) {
    return <p className="text-destructive text-sm">Failed to load profile list.</p>
  }
  return (
    <p className="text-muted-foreground text-sm">
      Select a profile from the left to edit its YAML.
    </p>
  )
}

export function ProfileIndexPage(): React.JSX.Element {
  const { data, isPending, isError } = useConfigFileList('profiles')
  return (
    <div className="container mx-auto flex gap-6 px-4 py-6">
      <div className="w-48 shrink-0">
        <FilePicker
          family="profiles"
          slugs={data?.slugs ?? []}
          activeSlug=""
          basePath="/config/profiles"
        />
      </div>
      <div className="flex-1">
        <h1 className="mb-4 text-xl font-semibold">Profiles</h1>
        <IndexStatus isPending={isPending} isError={isError} />
      </div>
    </div>
  )
}
