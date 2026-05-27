// Config git-history page — /config/history (ALP-684 story 06c).
//
// Layout:
//   Top  — file picker (text input, relative path under config/)
//   Left — commits list for the selected file (git log)
//   Right— diff viewer: select "from" and "to" commits to view the diff
//
// Untracked or uncommitted-changes badge: shown via GET /git/status.

import { useState } from 'react'

import { type GitCommitEntry, useGitDiff, useGitHistory, useGitStatus } from '@/api/config-views'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

// ---------------------------------------------------------------------------
// File picker
// ---------------------------------------------------------------------------

type FilePickerProps = {
  value: string
  onChange: (v: string) => void
}

function FilePicker({ value, onChange }: FilePickerProps): React.JSX.Element {
  const [draft, setDraft] = useState(value)

  const submit = (): void => {
    const trimmed = draft.trim()
    if (trimmed.length > 0) {
      onChange(trimmed)
    }
  }

  return (
    <div className="flex items-center gap-2">
      <label htmlFor="config-file-picker" className="text-sm font-medium whitespace-nowrap">
        Config file
      </label>
      <input
        id="config-file-picker"
        type="text"
        className="flex-1 rounded border px-2 py-1 font-mono text-sm focus:ring-1 focus:outline-none"
        placeholder="e.g. guardrails.yaml or profiles/default.yaml"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            submit()
          }
        }}
      />
      <button
        type="button"
        className="hover:bg-muted rounded border px-3 py-1 text-sm transition-colors"
        onClick={submit}
      >
        Load
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Git-status badge
// ---------------------------------------------------------------------------

function StatusBadge({ file }: { file: string }): React.JSX.Element {
  const { data, isPending } = useGitStatus(file)

  if (isPending || !data) {
    return <span />
  }
  if (data.untracked) {
    return (
      <span className="rounded bg-yellow-100 px-2 py-0.5 text-xs font-semibold text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200">
        untracked
      </span>
    )
  }
  if (!data.tracked) {
    return (
      <span className="rounded bg-slate-100 px-2 py-0.5 text-xs font-semibold text-slate-600 dark:bg-slate-800 dark:text-slate-400">
        not in git
      </span>
    )
  }
  if (data.has_uncommitted_changes) {
    return (
      <span className="rounded bg-orange-100 px-2 py-0.5 text-xs font-semibold text-orange-800 dark:bg-orange-900 dark:text-orange-200">
        uncommitted changes
      </span>
    )
  }
  return (
    <span className="rounded bg-green-100 px-2 py-0.5 text-xs font-semibold text-green-800 dark:bg-green-900 dark:text-green-200">
      tracked
    </span>
  )
}

// ---------------------------------------------------------------------------
// Single commit row
// ---------------------------------------------------------------------------

type CommitRowProps = {
  commit: GitCommitEntry
  isFrom: boolean
  isTo: boolean
  onSelectFrom: (sha: string) => void
  onSelectTo: (sha: string) => void
}

function commitHighlight(isFrom: boolean, isTo: boolean): string {
  if (isFrom) {
    return 'bg-blue-50 dark:bg-blue-950'
  }
  if (isTo) {
    return 'bg-green-50 dark:bg-green-950'
  }
  return ''
}

function CommitRow({
  commit,
  isFrom,
  isTo,
  onSelectFrom,
  onSelectTo,
}: CommitRowProps): React.JSX.Element {
  const highlightClass = commitHighlight(isFrom, isTo)

  return (
    <div
      className={`flex items-start gap-2 rounded px-2 py-1.5 transition-colors ${highlightClass}`}
    >
      <div className="min-w-0 flex-1">
        <p className="text-muted-foreground font-mono text-xs">{commit.short_sha}</p>
        <p className="truncate text-sm">{commit.subject}</p>
        <p className="text-muted-foreground text-xs">
          {commit.author} · {commit.date}
        </p>
      </div>
      <div className="flex shrink-0 gap-1 pt-0.5">
        <button
          type="button"
          title="Set as from (older) commit"
          className={`rounded border px-1.5 py-0.5 text-xs transition-colors ${isFrom ? 'border-blue-300 bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-200' : 'hover:bg-muted'}`}
          onClick={() => onSelectFrom(commit.sha)}
        >
          from
        </button>
        <button
          type="button"
          title="Set as to (newer) commit"
          className={`rounded border px-1.5 py-0.5 text-xs transition-colors ${isTo ? 'border-green-300 bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-200' : 'hover:bg-muted'}`}
          onClick={() => onSelectTo(commit.sha)}
        >
          to
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Commits list
// ---------------------------------------------------------------------------

type CommitsListProps = {
  file: string
  fromSha: string | null
  toSha: string | null
  onSelectFrom: (sha: string) => void
  onSelectTo: (sha: string) => void
}

function CommitsList({
  file,
  fromSha,
  toSha,
  onSelectFrom,
  onSelectTo,
}: CommitsListProps): React.JSX.Element {
  const { data, isPending, isError } = useGitHistory(file)

  if (isPending) {
    return <p className="text-muted-foreground p-2 text-sm">Loading history…</p>
  }
  if (isError) {
    return <p className="text-destructive p-2 text-sm">Failed to load git history.</p>
  }
  if (data.commits.length === 0) {
    return (
      <p className="text-muted-foreground p-2 text-sm italic">No commits found for this file.</p>
    )
  }
  return (
    <div className="space-y-0.5">
      {data.commits.map((commit) => (
        <CommitRow
          key={commit.sha}
          commit={commit}
          isFrom={fromSha === commit.sha}
          isTo={toSha === commit.sha}
          onSelectFrom={onSelectFrom}
          onSelectTo={onSelectTo}
        />
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Diff viewer
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

function DiffPane({
  file,
  fromSha,
  toSha,
}: {
  file: string
  fromSha: string
  toSha: string
}): React.JSX.Element {
  const { data, isPending, isError } = useGitDiff(file, fromSha, toSha)

  if (isPending) {
    return <p className="text-muted-foreground p-2 text-sm">Loading diff…</p>
  }
  if (isError) {
    return <p className="text-destructive p-2 text-sm">Failed to load diff.</p>
  }
  if (data.diff_text.length === 0) {
    return (
      <p className="text-muted-foreground p-2 text-sm italic">
        No changes between these two commits.
      </p>
    )
  }
  const lines = data.diff_text.split('\n')
  return (
    <pre className="bg-muted overflow-x-auto rounded-md p-4 font-mono text-xs leading-5">
      {lines.map((line, i) => (
        // index is stable for static diff content
        // eslint-disable-next-line react/no-array-index-key
        <DiffLine key={i} line={line} />
      ))}
    </pre>
  )
}

// ---------------------------------------------------------------------------
// Page entry point
// ---------------------------------------------------------------------------

export function ConfigHistoryPage(): React.JSX.Element {
  const [file, setFile] = useState('guardrails.yaml')
  const [fromSha, setFromSha] = useState<string | null>(null)
  const [toSha, setToSha] = useState<string | null>(null)
  const hasDiff = fromSha !== null && toSha !== null && fromSha !== toSha

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Config Git History</h1>
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <FilePicker value={file} onChange={setFile} />
        </div>
        <StatusBadge file={file} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <CommitsCard
          file={file}
          fromSha={fromSha}
          toSha={toSha}
          onSelectFrom={setFromSha}
          onSelectTo={setToSha}
        />
        <DiffCard file={file} fromSha={fromSha} toSha={toSha} hasDiff={hasDiff} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Card wrappers (keep ConfigHistoryPage under 50 lines)
// ---------------------------------------------------------------------------

type CommitsCardProps = {
  file: string
  fromSha: string | null
  toSha: string | null
  onSelectFrom: (sha: string) => void
  onSelectTo: (sha: string) => void
}

function CommitsCard({
  file,
  fromSha,
  toSha,
  onSelectFrom,
  onSelectTo,
}: CommitsCardProps): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Commits
          <span className="text-muted-foreground ml-2 font-mono text-xs font-normal">{file}</span>
        </CardTitle>
      </CardHeader>
      <CardContent className="max-h-[60vh] overflow-y-auto">
        <CommitsList
          file={file}
          fromSha={fromSha}
          toSha={toSha}
          onSelectFrom={onSelectFrom}
          onSelectTo={onSelectTo}
        />
      </CardContent>
    </Card>
  )
}

type DiffCardProps = {
  file: string
  fromSha: string | null
  toSha: string | null
  hasDiff: boolean
}

function DiffCard({ file, fromSha, toSha, hasDiff }: DiffCardProps): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Diff
          {hasDiff ? (
            <span className="text-muted-foreground ml-2 font-mono text-xs font-normal">
              {fromSha?.slice(0, 7)} → {toSha?.slice(0, 7)}
            </span>
          ) : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="max-h-[60vh] overflow-y-auto">
        {hasDiff && fromSha !== null && toSha !== null ? (
          <DiffPane file={file} fromSha={fromSha} toSha={toSha} />
        ) : (
          <p className="text-muted-foreground text-sm italic">
            Select a &ldquo;from&rdquo; and &ldquo;to&rdquo; commit from the history on the left to
            view the diff.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
