// StringArrayTagEditor — chip-based add/remove editor (ALP-679).
// Used for fields like profile.active_sectors and the sector ticker arrays
// in assets.yaml. Each tag renders as a chip with a remove control; new
// tags land via a small inline input + add button.

import { useState } from 'react'

import { cn } from '@/lib/utils'

type StringArrayTagEditorProps = {
  path: string
  label: string
  value: readonly string[]
  onChange: (next: string[]) => void
  disabled?: boolean
  className?: string
}

function TagChip({
  tag,
  disabled,
  onRemove,
}: {
  tag: string
  disabled: boolean | undefined
  onRemove: () => void
}): React.JSX.Element {
  return (
    <span
      role="listitem"
      className="border-border bg-muted inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium"
    >
      <span>{tag}</span>
      <button
        type="button"
        aria-label={`Remove ${tag}`}
        disabled={disabled}
        onClick={onRemove}
        className="hover:text-destructive ml-1 cursor-pointer text-xs disabled:cursor-not-allowed disabled:opacity-50"
      >
        ×
      </button>
    </span>
  )
}

function AddTagRow({
  draft,
  disabled,
  onDraftChange,
  onAdd,
}: {
  draft: string
  disabled: boolean | undefined
  onDraftChange: (next: string) => void
  onAdd: () => void
}): React.JSX.Element {
  return (
    <div className="flex items-center gap-2">
      <input
        type="text"
        value={draft}
        placeholder="Add tag"
        disabled={disabled}
        onChange={(e) => {
          onDraftChange(e.target.value)
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            onAdd()
          }
        }}
        className="border-input bg-background ring-offset-background focus-visible:ring-ring h-10 flex-1 rounded-md border px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
      />
      <button
        type="button"
        disabled={disabled}
        onClick={onAdd}
        className="bg-primary text-primary-foreground hover:bg-primary/90 h-10 cursor-pointer rounded-md px-4 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50"
      >
        Add
      </button>
    </div>
  )
}

export function StringArrayTagEditor({
  path,
  label,
  value,
  onChange,
  disabled,
  className,
}: StringArrayTagEditorProps): React.JSX.Element {
  const [draft, setDraft] = useState('')
  const handleAdd = (): void => {
    const trimmed = draft.trim()
    if (trimmed === '') {
      return
    }
    onChange([...value, trimmed])
    setDraft('')
  }
  const handleRemove = (target: string): void => {
    onChange(value.filter((existing) => existing !== target))
  }
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <span id={`${path}-label`} className="text-sm font-medium">
        {label}
      </span>
      <div
        role="list"
        aria-labelledby={`${path}-label`}
        className="flex flex-wrap items-center gap-2"
      >
        {value.map((tag) => (
          <TagChip
            key={tag}
            tag={tag}
            disabled={disabled}
            onRemove={() => {
              handleRemove(tag)
            }}
          />
        ))}
      </div>
      <AddTagRow draft={draft} disabled={disabled} onDraftChange={setDraft} onAdd={handleAdd} />
    </div>
  )
}
