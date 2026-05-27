// Invocation detail page — /history/$invocationId (ALP-674).
//
// Renders the full per-invocation graph: header, archive files, PM envelopes,
// commands and fills. Reference-ID chips in PM narrative open the brief-viewer
// side panel.

import { useState } from 'react'

import { useNavigate } from '@tanstack/react-router'

import { useInvocationDetail } from '@/api/queries'

import { BriefViewerPanel } from '../brief-viewer'
import { ArchivePane } from './archive-pane'
import { CommandsPane } from './commands-pane'
import { HeaderPane } from './header-pane'
import { PmPane } from './pm-pane'
import type { RefPrefix } from './ref-id-utils'

const BACK_SEARCH = {
  date_from: undefined,
  date_to: undefined,
  run_type: undefined,
  status: undefined,
  page: undefined,
  page_size: undefined,
} as const

type LoadingProps = { navigate: ReturnType<typeof useNavigate>; isError: boolean }

function DetailLoadState({ navigate, isError }: LoadingProps): React.JSX.Element {
  if (!isError) {
    return <div className="text-muted-foreground p-6 text-sm">Loading invocation detail…</div>
  }
  return (
    <div className="p-6">
      <p className="text-red-600">Failed to load invocation detail.</p>
      <button
        type="button"
        onClick={() => void navigate({ to: '/history', search: BACK_SEARCH })}
        className="mt-2 text-sm underline"
      >
        Back to run history
      </button>
    </div>
  )
}

type InvocationDetailPageProps = { invocationId: string }

export function InvocationDetailPage({
  invocationId,
}: InvocationDetailPageProps): React.JSX.Element {
  const navigate = useNavigate()
  const { data, isLoading, isError } = useInvocationDetail(invocationId)
  const [activeBriefPrefix, setActiveBriefPrefix] = useState<RefPrefix | null>(null)

  if (isLoading || isError || data === undefined) {
    return <DetailLoadState navigate={navigate} isError={isError} />
  }

  return (
    <InvocationDetailContent
      invocationId={invocationId}
      data={data}
      navigate={navigate}
      activeBriefPrefix={activeBriefPrefix}
      setActiveBriefPrefix={setActiveBriefPrefix}
    />
  )
}

type ContentProps = {
  invocationId: string
  data: NonNullable<ReturnType<typeof useInvocationDetail>['data']>
  navigate: ReturnType<typeof useNavigate>
  activeBriefPrefix: RefPrefix | null
  setActiveBriefPrefix: (p: RefPrefix | null) => void
}

function InvocationDetailContent({
  invocationId,
  data,
  navigate,
  activeBriefPrefix,
  setActiveBriefPrefix,
}: ContentProps): React.JSX.Element {
  return (
    <div className="relative">
      <div className="mb-4">
        <button
          type="button"
          onClick={() => void navigate({ to: '/history', search: BACK_SEARCH })}
          className="text-muted-foreground hover:text-foreground text-sm underline"
        >
          ← Run history
        </button>
      </div>
      <h1 className="mb-6 text-2xl font-semibold">
        Invocation <span className="font-mono text-lg">{invocationId}</span>
      </h1>
      <div className="space-y-8">
        <HeaderPane header={data.header} />
        <ArchivePane data={data} />
        <PmPane
          entries={data.pm_entries}
          onChipClick={(_refId, prefix) => setActiveBriefPrefix(prefix)}
        />
        <CommandsPane entries={data.command_fill_entries} />
      </div>
      <BriefViewerPanel
        invocationId={invocationId}
        refPrefix={activeBriefPrefix}
        onClose={() => setActiveBriefPrefix(null)}
      />
    </div>
  )
}
