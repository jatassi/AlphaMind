// FormComposer — schema-driven form renderer (ALP-679).
//
// Walks the FormSchema's `fields` array; for each field, reads the
// current value at the dotted path and dispatches to the matching
// per-control component. Edits propagate via onChange with the path's
// nested location updated.

import { BooleanToggle } from './boolean-toggle'
import { CronExpressionPicker } from './cron-expression-picker'
import { EnumDropdown } from './enum-dropdown'
import { NumberInput } from './number-input'
import { ObjectArrayTableEditor } from './object-array-table-editor'
import { PathInput } from './path-input'
import { ReloadPolicyBadge } from './reload-policy-badge'
import { StringArrayTagEditor } from './string-array-tag-editor'
import { StringInput } from './string-input'
import type { FormFieldSchema, FormSchema } from './types'

type FormComposerProps = {
  schema: FormSchema
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
}

function readPath(payload: Record<string, unknown>, path: string): unknown {
  const segments = path.split('.')
  let cursor: unknown = payload
  for (const seg of segments) {
    if (cursor && typeof cursor === 'object' && seg in (cursor as Record<string, unknown>)) {
      cursor = (cursor as Record<string, unknown>)[seg]
    } else {
      return undefined
    }
  }
  return cursor
}

function writePath(
  payload: Record<string, unknown>,
  path: string,
  next: unknown,
): Record<string, unknown> {
  const segments = path.split('.')
  const root: Record<string, unknown> = { ...payload }
  let cursor: Record<string, unknown> = root
  for (let i = 0; i < segments.length - 1; i += 1) {
    const seg = segments[i]
    const existing = cursor[seg]
    const copy: Record<string, unknown> =
      existing && typeof existing === 'object' && !Array.isArray(existing)
        ? { ...(existing as Record<string, unknown>) }
        : {}
    cursor[seg] = copy
    cursor = copy
  }
  // `segments` came from a non-empty path, so the last segment is always
  // defined; the `?? ''` keeps TS happy without a non-null assertion.
  cursor[segments.at(-1) ?? ''] = next
  return root
}

function asNumber(raw: unknown): number {
  return typeof raw === 'number' ? raw : 0
}

function asString(raw: unknown): string {
  return typeof raw === 'string' ? raw : ''
}

function asBoolean(raw: unknown): boolean {
  return raw === true
}

function asStringArray(raw: unknown): readonly string[] {
  return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string') : []
}

function asObjectArray(raw: unknown): readonly Record<string, unknown>[] {
  if (!Array.isArray(raw)) {
    return []
  }
  return raw.filter(
    (item): item is Record<string, unknown> =>
      typeof item === 'object' && item !== null && !Array.isArray(item),
  )
}

function renderStringControl(
  field: FormFieldSchema,
  rawValue: unknown,
  onChange: (next: unknown) => void,
): React.JSX.Element {
  return (
    <StringInput
      path={field.path}
      label={field.path}
      value={asString(rawValue)}
      onChange={onChange}
      constraints={{
        pattern: field.constraints.pattern,
        minLength: field.constraints.minLength,
        maxLength: field.constraints.maxLength,
      }}
    />
  )
}

const DISPATCH: Record<
  FormFieldSchema['control_type'],
  (
    field: FormFieldSchema,
    rawValue: unknown,
    onChange: (next: unknown) => void,
  ) => React.JSX.Element
> = {
  number: (field, rawValue, onChange) => (
    <NumberInput
      path={field.path}
      label={field.path}
      value={asNumber(rawValue)}
      onChange={onChange}
      constraints={{ minimum: field.constraints.minimum, maximum: field.constraints.maximum }}
    />
  ),
  boolean: (field, rawValue, onChange) => (
    <BooleanToggle
      path={field.path}
      label={field.path}
      value={asBoolean(rawValue)}
      onChange={onChange}
    />
  ),
  enum: (field, rawValue, onChange) => (
    <EnumDropdown
      path={field.path}
      label={field.path}
      value={asString(rawValue)}
      onChange={onChange}
      choices={field.constraints.enum_choices ?? []}
    />
  ),
  'string-array': (field, rawValue, onChange) => (
    <StringArrayTagEditor
      path={field.path}
      label={field.path}
      value={asStringArray(rawValue)}
      onChange={onChange}
    />
  ),
  'object-array': (field, rawValue, onChange) => (
    <ObjectArrayTableEditor
      path={field.path}
      label={field.path}
      value={asObjectArray(rawValue)}
      columns={[]}
      onChange={onChange}
    />
  ),
  cron: (field, rawValue, onChange) => (
    <CronExpressionPicker
      path={field.path}
      label={field.path}
      value={asString(rawValue)}
      onChange={onChange}
    />
  ),
  path: (field, rawValue, onChange) => (
    <PathInput
      path={field.path}
      label={field.path}
      value={asString(rawValue)}
      onChange={onChange}
    />
  ),
  string: renderStringControl,
}

function FieldRow({
  field,
  value,
  onChange,
}: {
  field: FormFieldSchema
  value: Record<string, unknown>
  onChange: (next: Record<string, unknown>) => void
}): React.JSX.Element {
  const rawValue = readPath(value, field.path)
  const handleChange = (next: unknown): void => {
    onChange(writePath(value, field.path, next))
  }
  // DISPATCH covers every member of FormFieldSchema['control_type']; the
  // lookup is exhaustive by construction, but pulling the function via
  // bracket access keeps the inner control list flat.
  const render = DISPATCH[field.control_type]
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        <ReloadPolicyBadge policy={field.reload_policy} />
      </div>
      {render(field, rawValue, handleChange)}
    </div>
  )
}

export function FormComposer({ schema, value, onChange }: FormComposerProps): React.JSX.Element {
  return (
    <div className="flex flex-col gap-4">
      {schema.fields.map((field) => (
        <FieldRow key={field.path} field={field} value={value} onChange={onChange} />
      ))}
    </div>
  )
}
