// Archive pane for the per-invocation detail page (ALP-674).
//
// Renders the archive sections (distillation, analysis, decision, execution)
// with their file listings. Each file is a link that fetches the archive file
// content from the backend streaming endpoint.

import { useState } from 'react'

import type { ArchiveSection, InvocationDetailResponse } from '@/api/queries'

type FileLinkProps = {
  invocationId: string
  section: string
  filename: string
}

function FileLink({ invocationId, section, filename }: FileLinkProps): React.JSX.Element {
  const [expanded, setExpanded] = useState(false)
  const [content, setContent] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  async function handleClick(): Promise<void> {
    if (expanded) {
      setExpanded(false)
      return
    }
    if (content !== null) {
      setExpanded(true)
      return
    }
    setLoading(true)
    try {
      const url = `/api/views/history/runs/${encodeURIComponent(invocationId)}/archive/${encodeURIComponent(section)}/${encodeURIComponent(filename)}`
      const resp = await fetch(url, { credentials: 'same-origin' })
      if (resp.ok) {
        const text = await resp.text()
        setContent(text)
        setExpanded(true)
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={() => void handleClick()}
        className="font-mono text-xs text-blue-600 hover:underline"
      >
        {loading ? 'Loading…' : filename}
      </button>
      {expanded && content !== null ? (
        <pre className="bg-muted mt-1 max-h-64 overflow-auto rounded border p-2 text-xs whitespace-pre-wrap">
          {content}
        </pre>
      ) : null}
    </div>
  )
}

type SectionCardProps = {
  invocationId: string
  section: ArchiveSection
}

function SectionCard({ invocationId, section }: SectionCardProps): React.JSX.Element {
  return (
    <div className="rounded border p-3">
      <h3 className="mb-2 font-medium capitalize">{section.section}</h3>
      {section.files.length === 0 ? (
        <span className="text-muted-foreground text-xs">No files.</span>
      ) : (
        <ul className="space-y-1">
          {section.files.map((f) => (
            <li key={f}>
              <FileLink invocationId={invocationId} section={section.section} filename={f} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

type ArchivePaneProps = {
  data: InvocationDetailResponse
}

export function ArchivePane({ data }: ArchivePaneProps): React.JSX.Element {
  const { header, archive_sections } = data
  return (
    <section aria-label="Archive files">
      <h2 className="mb-2 text-lg font-semibold">Archive files</h2>
      {archive_sections.length === 0 ? (
        <p className="text-muted-foreground text-sm">
          No archive directory found for this invocation.
        </p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {archive_sections.map((s) => (
            <SectionCard key={s.section} invocationId={header.invocation_id} section={s} />
          ))}
        </div>
      )}
    </section>
  )
}
