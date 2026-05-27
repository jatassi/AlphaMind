// Reference-ID chip component (ALP-674).
//
// Renders a styled chip for a reference ID like SA-TECH-3, QR-4, AR-2, CR-1.
// Clicking opens the brief viewer side panel with the matching prefix.

import { extractRefPrefix, REF_ID_PATTERN, type RefPrefix } from './ref-id-utils'

type RefIdChipProps = {
  refId: string
  onClick: (refId: string, prefix: RefPrefix) => void
}

export function RefIdChip({ refId, onClick }: RefIdChipProps): React.JSX.Element {
  const prefix = extractRefPrefix(refId)
  if (prefix === null) {
    return <span className="font-mono text-xs">{refId}</span>
  }

  return (
    <button
      type="button"
      onClick={() => onClick(refId, prefix)}
      className="inline-flex items-center rounded bg-blue-50 px-1.5 py-0.5 font-mono text-xs text-blue-700 hover:bg-blue-100 focus:ring-1 focus:ring-blue-400 focus:outline-none"
      aria-label={`View brief for ${refId}`}
    >
      {refId}
    </button>
  )
}

type NarrativeWithChipsProps = {
  text: string
  onChipClick: (refId: string, prefix: RefPrefix) => void
}

// Renders narrative text with inline [PREFIX-N] patterns replaced by
// RefIdChip buttons.
export function NarrativeWithChips({
  text,
  onChipClick,
}: NarrativeWithChipsProps): React.JSX.Element {
  const pattern = new RegExp(REF_ID_PATTERN.source, REF_ID_PATTERN.flags)
  const parts: React.ReactNode[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = pattern.exec(text)) !== null) {
    const before = text.slice(lastIndex, match.index)
    if (before) {
      parts.push(before)
    }
    const refId = match[0].slice(1, -1) // strip [ ]
    parts.push(
      <RefIdChip key={`${refId}-${String(match.index)}`} refId={refId} onClick={onChipClick} />,
    )
    lastIndex = match.index + match[0].length
  }
  const rest = text.slice(lastIndex)
  if (rest) {
    parts.push(rest)
  }
  // eslint-disable-next-line react/jsx-no-useless-fragment -- array of nodes needs a wrapper
  return <>{parts}</>
}
