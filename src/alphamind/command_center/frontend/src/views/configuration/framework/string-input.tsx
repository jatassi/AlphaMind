// StringInput — text input with regex validation pass-through (ALP-679).
// The HTML5 `pattern` attribute lets the browser surface a constraint
// failure inline; the FormComposer reads the same constraint to gate the
// SaveAction client-side.

import { cn } from '@/lib/utils'

type StringInputProps = {
  path: string
  label: string
  value: string
  onChange: (next: string) => void
  constraints?: {
    pattern?: string
    minLength?: number
    maxLength?: number
  }
  disabled?: boolean
  className?: string
}

export function StringInput({
  path,
  label,
  value,
  onChange,
  constraints,
  disabled,
  className,
}: StringInputProps): React.JSX.Element {
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label htmlFor={path} className="text-sm font-medium">
        {label}
      </label>
      <input
        id={path}
        type="text"
        value={value}
        disabled={disabled}
        pattern={constraints?.pattern}
        minLength={constraints?.minLength}
        maxLength={constraints?.maxLength}
        onChange={(e) => {
          onChange(e.target.value)
        }}
        className="border-input bg-background ring-offset-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
      />
    </div>
  )
}
