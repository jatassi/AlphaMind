// Resolved-config viewer page — /config/resolved (ALP-684 story 06c).
//
// Left pane  — the merged resolved_config.json bundle for the selected (or
//              most-recent) invocation, rendered as pretty-printed JSON.
// Right pane — per-source-file YAML content, one collapsible section per
//              referenced file (profile, regime, overlay, mode).
//
// "Diff with previous" button opens the DiffModal so the operator can compare
// two invocations' bundles via the /resolved/diff endpoint.

import { useState } from 'react'

import { type ResolvedConfigBundle, useResolvedConfig } from '@/api/config-views'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

import { DiffModal } from './diff-modal'

// ---------------------------------------------------------------------------
// Skeleton states
// ---------------------------------------------------------------------------

function LoadingState(): React.JSX.Element {
  return (
    <div className="flex items-center justify-center p-8">
      <span className="text-muted-foreground">Loading resolved config…</span>
    </div>
  )
}

function ErrorState(): React.JSX.Element {
  return (
    <div className="flex items-center justify-center p-8">
      <span className="text-destructive">Failed to load resolved config.</span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Source-file collapsible section
// ---------------------------------------------------------------------------

function SourceFileSection({
  path,
  content,
}: {
  path: string
  content: string
}): React.JSX.Element {
  const [open, setOpen] = useState(false)

  return (
    <div className="rounded-md border">
      <button
        type="button"
        className="hover:bg-muted/50 flex w-full items-center justify-between px-4 py-2 text-sm font-medium transition-colors"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="font-mono text-xs">{path}</span>
        <span className="text-muted-foreground text-xs">{open ? '▲' : '▼'}</span>
      </button>
      {open ? (
        <div className="border-t px-4 py-3">
          {content.length === 0 ? (
            <p className="text-muted-foreground text-xs italic">File not found or empty.</p>
          ) : (
            <pre className="overflow-x-auto text-xs whitespace-pre-wrap">{content}</pre>
          )}
        </div>
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Panes
// ---------------------------------------------------------------------------

function BundlePane({ bundle }: { bundle: Record<string, unknown> }): React.JSX.Element {
  return (
    <pre className="bg-muted overflow-x-auto rounded-md p-4 text-xs leading-5 whitespace-pre-wrap">
      {JSON.stringify(bundle, null, 2)}
    </pre>
  )
}

function SourceFilesPane({
  sourceFiles,
}: {
  sourceFiles: Record<string, string>
}): React.JSX.Element {
  const entries = Object.entries(sourceFiles)
  if (entries.length === 0) {
    return (
      <p className="text-muted-foreground text-sm italic">
        No source files referenced by this bundle.
      </p>
    )
  }
  return (
    <div className="space-y-2">
      {entries.map(([path, content]) => (
        <SourceFileSection key={path} path={path} content={content} />
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main content (shown after data loads)
// ---------------------------------------------------------------------------

function ResolvedConfigContent({
  data,
  onDiffClick,
}: {
  data: ResolvedConfigBundle
  onDiffClick: () => void
}): React.JSX.Element {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Resolved Config</h1>
          <p className="text-muted-foreground mt-1 font-mono text-sm">{data.invocation_id}</p>
        </div>
        <button
          type="button"
          className="hover:bg-muted rounded-md border px-3 py-1.5 text-sm transition-colors"
          onClick={onDiffClick}
        >
          Diff with previous
        </button>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Resolved bundle</CardTitle>
          </CardHeader>
          <CardContent>
            <BundlePane bundle={data.bundle} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Source files</CardTitle>
          </CardHeader>
          <CardContent>
            <SourceFilesPane sourceFiles={data.source_files} />
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page entry point
// ---------------------------------------------------------------------------

export function ResolvedConfigPage(): React.JSX.Element {
  const { data, isPending, isError } = useResolvedConfig()
  const [showDiff, setShowDiff] = useState(false)

  if (isPending) {
    return <LoadingState />
  }
  if (isError) {
    return <ErrorState />
  }

  return (
    <>
      <ResolvedConfigContent data={data} onDiffClick={() => setShowDiff(true)} />
      {showDiff ? (
        <DiffModal defaultToId={data.invocation_id} onClose={() => setShowDiff(false)} />
      ) : null}
    </>
  )
}
