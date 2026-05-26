// Brief viewer component (ALP-674).
//
// Displays brief sections from the source-brief retrieval store for a given
// invocation_id and ref_prefix. Used both as a standalone route and as the
// content of the brief-viewer side panel.

import { useBriefRetrieval } from '@/api/queries'

type BriefViewerProps = {
  invocationId: string
  refPrefix: string
}

export function BriefViewer({ invocationId, refPrefix }: BriefViewerProps): React.JSX.Element {
  const { data, isLoading, isError } = useBriefRetrieval(invocationId, refPrefix)

  if (isLoading) {
    return <p className="text-muted-foreground p-4 text-sm">Loading brief…</p>
  }
  if (isError || data === undefined) {
    return (
      <p className="p-4 text-sm text-red-600">
        Failed to load brief. The archive may not be available.
      </p>
    )
  }
  if (data.sections.length === 0) {
    return (
      <p className="text-muted-foreground p-4 text-sm">
        No brief sections found for prefix <code>{refPrefix}</code>.
      </p>
    )
  }

  return (
    <div className="space-y-4 p-4">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground text-xs">Prefix:</span>
        <code className="rounded bg-blue-50 px-1.5 py-0.5 text-xs text-blue-700">{refPrefix}</code>
        <span className="text-muted-foreground text-xs">·</span>
        <span className="text-muted-foreground text-xs">{data.sections.length} section(s)</span>
      </div>
      {data.sections.map((section) => (
        <BriefSectionCard key={section.ref_id} refId={section.ref_id} content={section.content} />
      ))}
    </div>
  )
}

type BriefSectionCardProps = {
  refId: string
  content: string
}

function BriefSectionCard({ refId, content }: BriefSectionCardProps): React.JSX.Element {
  return (
    <div className="rounded border p-3">
      <div className="mb-2">
        <span className="rounded bg-blue-50 px-1.5 py-0.5 font-mono text-xs text-blue-700">
          {refId}
        </span>
      </div>
      <pre className="text-sm leading-relaxed whitespace-pre-wrap">{content}</pre>
    </div>
  )
}

// Side panel wrapper — slides in from the right over the detail page.
type BriefViewerPanelProps = {
  invocationId: string
  refPrefix: string | null
  onClose: () => void
}

export function BriefViewerPanel({
  invocationId,
  refPrefix,
  onClose,
}: BriefViewerPanelProps): React.JSX.Element | null {
  if (refPrefix === null) {
    return null
  }

  return (
    <div
      role="dialog"
      aria-label="Brief viewer"
      aria-modal="true"
      className="bg-background fixed inset-y-0 right-0 z-50 flex w-[480px] max-w-full flex-col border-l shadow-xl"
    >
      <div className="flex items-center justify-between border-b px-4 py-3">
        <h2 className="text-sm font-semibold">Source Brief — {refPrefix}</h2>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close brief viewer"
          className="text-muted-foreground hover:text-foreground rounded p-1"
        >
          ✕
        </button>
      </div>
      <div className="flex-1 overflow-y-auto">
        <BriefViewer invocationId={invocationId} refPrefix={refPrefix} />
      </div>
    </div>
  )
}
