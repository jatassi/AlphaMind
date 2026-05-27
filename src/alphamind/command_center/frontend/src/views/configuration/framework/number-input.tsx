// NumberInput — number-with-unit control for the config editor framework
// (ALP-679). Surfaces min/max enforcement from the schema's constraints
// dict and renders an optional unit suffix per docs/design/command-center.md
// § Config editor.

import { cn } from '@/lib/utils'

type NumberInputProps = {
  path: string
  label: string
  value: number
  onChange: (next: number) => void
  constraints: {
    minimum?: number
    maximum?: number
  }
  unit?: string
  disabled?: boolean
  className?: string
}

export function NumberInput({
  path,
  label,
  value,
  onChange,
  constraints,
  unit,
  disabled,
  className,
}: NumberInputProps): React.JSX.Element {
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label htmlFor={path} className="text-sm font-medium">
        {label}
      </label>
      <div className="flex items-center gap-2">
        <input
          id={path}
          type="number"
          value={Number.isFinite(value) ? value : ''}
          min={constraints.minimum}
          max={constraints.maximum}
          disabled={disabled}
          onChange={(e) => {
            const parsed = Number(e.target.value)
            // Non-numeric input falls through as NaN; downstream form
            // composer surfaces that as a parse-layer error rather than
            // emitting a NaN onChange call.
            if (Number.isFinite(parsed)) {
              onChange(parsed)
            }
          }}
          className="border-input bg-background ring-offset-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
        />
        {unit === undefined ? null : <span className="text-muted-foreground text-sm">{unit}</span>}
      </div>
    </div>
  )
}
