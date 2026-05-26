// Reference-ID chip — clickable chip that deep-links to the brief viewer (ALP-678).
//
// The brief viewer side panel (story 05d) is not yet built; the chip renders
// as a styled link whose href points to the future /history/briefs/$refId path.
// When 05d lands, the path just works without component changes.

type Props = {
  refId: string
}

export function ReferenceIdChip({ refId }: Props): React.JSX.Element {
  return (
    <a
      href={`/history/briefs/${refId}`}
      className="border-border hover:bg-muted inline-flex items-center gap-1 rounded border px-2 py-0.5 font-mono text-xs"
      title={`Open brief ${refId}`}
    >
      {refId}
    </a>
  )
}
