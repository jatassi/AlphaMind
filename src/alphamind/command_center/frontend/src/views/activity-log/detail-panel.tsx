// Row expansion panel — renders the typed detail_json for an activity-log row.
//
// The backend ships the raw JSON string from the DB. This panel pretty-prints
// the JSON payload. Entity-ID chips (position, thesis, order) deep-link to the
// matching detail view; stub destinations are used for views not yet built.

import type { ActivityLogRow } from '@/api/queries'

// Deep-link paths for entity chips. Destinations for unbuilt views use stubs.
const ENTITY_PATHS = {
  position: '/positions',
  thesis: '/theses',
  order: '/orders',
} as const

type EntityChipProps = {
  label: string
  id: string
  href: string
}

function EntityChip({ label, id, href }: EntityChipProps): React.JSX.Element {
  return (
    <a
      href={href}
      className="border-border hover:bg-muted inline-flex items-center gap-1 rounded border px-2 py-0.5 text-xs"
    >
      <span className="text-muted-foreground">{label}</span>
      <span className="font-mono">{id}</span>
    </a>
  )
}

function parseDetail(detailJson: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(detailJson) as unknown
    if (typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
    return { value: parsed }
  } catch {
    return { raw: detailJson }
  }
}

type Props = {
  row: ActivityLogRow
}

export function DetailPanel({ row }: Props): React.JSX.Element {
  const detail = parseDetail(row.detail_json)

  return (
    <div className="space-y-3 px-4 py-3">
      {/* Entity-ID chips — deep-link to the matching detail view */}
      <div className="flex flex-wrap gap-2">
        {row.position_id ? (
          <EntityChip
            label="position"
            id={row.position_id}
            href={`${ENTITY_PATHS.position}/${row.position_id}`}
          />
        ) : null}
        {row.thesis_id ? (
          <EntityChip
            label="thesis"
            id={row.thesis_id}
            href={`${ENTITY_PATHS.thesis}/${row.thesis_id}`}
          />
        ) : null}
        {row.order_id ? (
          <EntityChip
            label="order"
            id={row.order_id}
            href={`${ENTITY_PATHS.order}/${row.order_id}`}
          />
        ) : null}
      </div>

      {/* Detail JSON — typed payload */}
      <div className="border-border bg-muted/30 rounded border p-3">
        <p className="text-muted-foreground mb-1 text-xs font-medium">Event detail</p>
        <pre className="overflow-x-auto font-mono text-xs break-all whitespace-pre-wrap">
          {JSON.stringify(detail, null, 2)}
        </pre>
      </div>
    </div>
  )
}
