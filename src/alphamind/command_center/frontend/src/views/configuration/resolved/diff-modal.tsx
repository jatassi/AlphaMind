// Diff-with-previous modal for the resolved-config viewer (ALP-684 story 06c).
//
// Accepts two invocation IDs (from / to), fetches the unified-diff from
// GET /api/views/config/resolved/diff, and renders the lines in a coloured
// <pre> block.

import { useState } from 'react'

import { useResolvedConfigDiff } from '@/api/config-views'

// ---------------------------------------------------------------------------
// Diff line colouring
// ---------------------------------------------------------------------------

function DiffLine({ line }: { line: string }): React.JSX.Element {
  if (line.startsWith('+++') || line.startsWith('---')) {
    return <span className="text-foreground block font-semibold">{line}</span>
  }
  if (line.startsWith('+')) {
    return (
      <span className="block bg-green-50 text-green-800 dark:bg-green-950 dark:text-green-200">
        {line}
      </span>
    )
  }
  if (line.startsWith('-')) {
    return (
      <span className="block bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-200">{line}</span>
    )
  }
  if (line.startsWith('@@')) {
    return <span className="block text-blue-600 dark:text-blue-400">{line}</span>
  }
  return <span className="block">{line}</span>
}

function DiffBody({ diffLines }: { diffLines: string[] }): React.JSX.Element {
  if (diffLines.length === 0) {
    return <p className="text-muted-foreground text-sm">No changes between the two invocations.</p>
  }
  return (
    <pre className="bg-muted overflow-x-auto rounded-md p-4 font-mono text-xs leading-5">
      {diffLines.map((line, i) => (
        // index as key is safe — diff lines are static once fetched
        // eslint-disable-next-line react/no-array-index-key
        <DiffLine key={i} line={line} />
      ))}
    </pre>
  )
}

// ---------------------------------------------------------------------------
// Inner panel (needs both IDs to be non-empty)
// ---------------------------------------------------------------------------

function DiffPanel({ fromId, toId }: { fromId: string; toId: string }): React.JSX.Element {
  const { data, isPending, isError } = useResolvedConfigDiff(fromId, toId)
  if (isPending) {
    return <p className="text-muted-foreground text-sm">Computing diff…</p>
  }
  if (isError) {
    return (
      <p className="text-destructive text-sm">
        Failed to load diff — check that both invocation IDs exist.
      </p>
    )
  }
  return <DiffBody diffLines={data.diff_lines} />
}

// ---------------------------------------------------------------------------
// ID inputs row
// ---------------------------------------------------------------------------

type IdInputsProps = {
  fromId: string
  toId: string
  onFromChange: (v: string) => void
  onToChange: (v: string) => void
  onCompare: () => void
}

function IdInputs({
  fromId,
  toId,
  onFromChange,
  onToChange,
  onCompare,
}: IdInputsProps): React.JSX.Element {
  const canFetch = fromId.trim().length > 0 && toId.trim().length > 0
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label htmlFor="diff-from-id" className="mb-1 block text-xs font-medium">
            From invocation ID
          </label>
          <input
            id="diff-from-id"
            type="text"
            className="w-full rounded border px-2 py-1 font-mono text-xs focus:ring-1 focus:outline-none"
            placeholder="older invocation_id"
            value={fromId}
            onChange={(e) => onFromChange(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="diff-to-id" className="mb-1 block text-xs font-medium">
            To invocation ID
          </label>
          <input
            id="diff-to-id"
            type="text"
            className="w-full rounded border px-2 py-1 font-mono text-xs focus:ring-1 focus:outline-none"
            placeholder="newer invocation_id"
            value={toId}
            onChange={(e) => onToChange(e.target.value)}
          />
        </div>
      </div>
      <button
        type="button"
        disabled={!canFetch}
        className="hover:bg-muted rounded border px-3 py-1 text-sm transition-colors disabled:opacity-50"
        onClick={onCompare}
      >
        Compare
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Public component
// ---------------------------------------------------------------------------

type DiffModalProps = {
  /** Pre-fill the "to" field with the current invocation's ID. */
  defaultToId: string
  onClose: () => void
}

export function DiffModal({ defaultToId, onClose }: DiffModalProps): React.JSX.Element {
  const [fromId, setFromId] = useState('')
  const [toId, setToId] = useState(defaultToId)
  const [submitted, setSubmitted] = useState(false)
  const canFetch = fromId.trim().length > 0 && toId.trim().length > 0

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-background flex max-h-[80vh] w-full max-w-3xl flex-col space-y-4 rounded-lg p-6 shadow-xl">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Diff resolved configs</h2>
          <button
            type="button"
            className="text-muted-foreground hover:text-foreground text-lg leading-none transition-colors"
            onClick={onClose}
            aria-label="Close diff modal"
          >
            ×
          </button>
        </div>
        <IdInputs
          fromId={fromId}
          toId={toId}
          onFromChange={(v) => {
            setFromId(v)
            setSubmitted(false)
          }}
          onToChange={(v) => {
            setToId(v)
            setSubmitted(false)
          }}
          onCompare={() => setSubmitted(true)}
        />
        <div className="min-h-0 flex-1 overflow-auto">
          {submitted && canFetch ? (
            <DiffPanel fromId={fromId.trim()} toId={toId.trim()} />
          ) : (
            <p className="text-muted-foreground text-sm">
              Enter two invocation IDs above and click Compare.
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
