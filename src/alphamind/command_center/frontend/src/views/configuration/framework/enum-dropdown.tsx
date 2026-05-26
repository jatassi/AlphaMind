// EnumDropdown — populated from the schema's choices list (ALP-679).
// Renders a native <select> for full keyboard accessibility without
// pulling in Radix; per-view stories can swap to a fancier popover
// dropdown later.

import { cn } from '@/lib/utils'

type EnumDropdownProps = {
  path: string
  label: string
  value: string
  onChange: (next: string) => void
  choices: readonly string[]
  disabled?: boolean
  className?: string
}

export function EnumDropdown({
  path,
  label,
  value,
  onChange,
  choices,
  disabled,
  className,
}: EnumDropdownProps): React.JSX.Element {
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label htmlFor={path} className="text-sm font-medium">
        {label}
      </label>
      <select
        id={path}
        value={value}
        disabled={disabled}
        onChange={(e) => {
          onChange(e.target.value)
        }}
        className="border-input bg-background ring-offset-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
      >
        {choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </select>
    </div>
  )
}
