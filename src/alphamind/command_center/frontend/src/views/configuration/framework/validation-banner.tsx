// ValidationBanner — three-layer error banner (ALP-679).
//
// Renders nothing when every layer is empty. Otherwise renders one
// section per non-empty layer (Parse / Cross-reference / Semantic) with
// the per-row messages.

import { cn } from '@/lib/utils'

import type { ValidationLayerError, ValidationReport } from './types'

type ValidationBannerProps = {
  report: ValidationReport
  className?: string
}

function ErrorList({
  layerLabel,
  errors,
}: {
  layerLabel: string
  errors: readonly ValidationLayerError[]
}): React.JSX.Element | null {
  if (errors.length === 0) {
    return null
  }
  return (
    <div className="flex flex-col gap-1">
      <span className="text-destructive text-xs font-semibold uppercase">{layerLabel}</span>
      <ul className="list-disc pl-5 text-sm">
        {errors.map((err, idx) => (
          <li key={`${err.path}-${idx.toString()}`}>
            {err.path ? <span className="font-mono text-xs">{err.path}: </span> : null}
            <span>{err.message}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

export function ValidationBanner({
  report,
  className,
}: ValidationBannerProps): React.JSX.Element | null {
  const hasErrors =
    report.parse.length > 0 || report.cross_reference.length > 0 || report.semantic.length > 0
  if (!hasErrors) {
    return null
  }
  return (
    <div
      role="alert"
      className={cn(
        'border-destructive bg-destructive/10 flex flex-col gap-3 rounded-md border p-4',
        className,
      )}
    >
      <ErrorList layerLabel="Parse" errors={report.parse} />
      <ErrorList layerLabel="Cross-reference" errors={report.cross_reference} />
      <ErrorList layerLabel="Semantic" errors={report.semantic} />
    </div>
  )
}
