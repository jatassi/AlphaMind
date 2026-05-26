// BooleanToggle — checkbox-style toggle control for boolean YAML knobs
// (ALP-679). Renders as a checkbox so the test layer's getByLabelText +
// toBeChecked assertions work without a custom Switch primitive — visual
// polish can layer on later.

import { cn } from '@/lib/utils'

type BooleanToggleProps = {
  path: string
  label: string
  value: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  className?: string
}

export function BooleanToggle({
  path,
  label,
  value,
  onChange,
  disabled,
  className,
}: BooleanToggleProps): React.JSX.Element {
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <input
        id={path}
        type="checkbox"
        checked={value}
        disabled={disabled}
        onChange={(e) => {
          onChange(e.target.checked)
        }}
        className="border-input text-primary focus-visible:ring-ring h-4 w-4 rounded border focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
      />
      <label htmlFor={path} className="text-sm font-medium">
        {label}
      </label>
    </div>
  )
}
