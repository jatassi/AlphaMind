// ObjectArrayTableEditor — table editor for list[object] YAML fields (ALP-679).
// Per-cell controls dispatch on the column's declared control type. Row add /
// remove via toolbar buttons; the inner cell controls are intentionally narrow
// (string + number for v1) and per-file editors in 06a/06b can extend.
//
// Column ``key`` may be a dotted path (e.g. ``value.context_lower``) when the
// backend unfurls a ``dict[str, BaseModel]`` field — the cell then reads /
// writes through the nested object. The read + write helpers below treat a
// dotted ``key`` as path segments and a non-dotted ``key`` as a top-level
// property.

import { cn } from '@/lib/utils'

export type ObjectArrayColumn = {
  key: string
  label: string
  controlType: 'string' | 'number'
}

// Treat a dotted ``key`` as a sequence of object descents. ``key.split('.')``
// for ``value.context_lower`` yields ``['value', 'context_lower']``; the read
// helper walks the row following each segment.
function _readDottedCell(row: Record<string, unknown>, key: string): unknown {
  if (!key.includes('.')) {
    return row[key]
  }
  const segments = key.split('.')
  let current: unknown = row
  for (const seg of segments) {
    if (current === null || typeof current !== 'object') {
      return undefined
    }
    current = (current as Record<string, unknown>)[seg]
  }
  return current
}

// Immutably write a value into a (possibly nested) cell. Cloning each
// intermediate level mirrors the parent ``onChange`` contract — callers rely
// on referential inequality between successive row snapshots to drive
// re-render.
function _writeDottedCell(
  row: Record<string, unknown>,
  key: string,
  next: unknown,
): Record<string, unknown> {
  if (!key.includes('.')) {
    return { ...row, [key]: next }
  }
  const segments = key.split('.')
  const clone: Record<string, unknown> = { ...row }
  let cursor: Record<string, unknown> = clone
  for (let i = 0; i < segments.length - 1; i += 1) {
    const seg = segments[i]
    const existing = cursor[seg]
    const branch =
      existing !== null && typeof existing === 'object' && !Array.isArray(existing)
        ? { ...(existing as Record<string, unknown>) }
        : {}
    cursor[seg] = branch
    cursor = branch
  }
  cursor[segments.at(-1) ?? ''] = next
  return clone
}

type ObjectArrayTableEditorProps = {
  path: string
  label: string
  value: readonly Record<string, unknown>[]
  columns: readonly ObjectArrayColumn[]
  onChange: (next: Record<string, unknown>[]) => void
  disabled?: boolean
  className?: string
}

function emptyRow(columns: readonly ObjectArrayColumn[]): Record<string, unknown> {
  let row: Record<string, unknown> = {}
  for (const col of columns) {
    const initial = col.controlType === 'number' ? 0 : ''
    row = _writeDottedCell(row, col.key, initial)
  }
  return row
}

function cellToString(cellValue: unknown): string {
  if (typeof cellValue === 'string') {
    return cellValue
  }
  if (cellValue === null || cellValue === undefined) {
    return ''
  }
  if (typeof cellValue === 'number' || typeof cellValue === 'boolean') {
    return String(cellValue)
  }
  return ''
}

function NumberCellInput({
  cellValue,
  disabled,
  onCellChange,
}: {
  cellValue: unknown
  disabled: boolean | undefined
  onCellChange: (next: unknown) => void
}): React.JSX.Element {
  return (
    <input
      type="number"
      value={typeof cellValue === 'number' ? cellValue : 0}
      disabled={disabled}
      onChange={(e) => {
        const parsed = Number(e.target.value)
        if (Number.isFinite(parsed)) {
          onCellChange(parsed)
        }
      }}
      className="border-input bg-background h-9 w-full rounded-md border px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-50"
    />
  )
}

function StringCellInput({
  cellValue,
  disabled,
  onCellChange,
}: {
  cellValue: unknown
  disabled: boolean | undefined
  onCellChange: (next: unknown) => void
}): React.JSX.Element {
  return (
    <input
      type="text"
      value={cellToString(cellValue)}
      disabled={disabled}
      onChange={(e) => {
        onCellChange(e.target.value)
      }}
      className="border-input bg-background h-9 w-full rounded-md border px-2 py-1 text-sm disabled:cursor-not-allowed disabled:opacity-50"
    />
  )
}

function CellInput({
  column,
  cellValue,
  disabled,
  onCellChange,
}: {
  column: ObjectArrayColumn
  cellValue: unknown
  disabled: boolean | undefined
  onCellChange: (next: unknown) => void
}): React.JSX.Element {
  if (column.controlType === 'number') {
    return <NumberCellInput cellValue={cellValue} disabled={disabled} onCellChange={onCellChange} />
  }
  return <StringCellInput cellValue={cellValue} disabled={disabled} onCellChange={onCellChange} />
}

function TableHeader({ columns }: { columns: readonly ObjectArrayColumn[] }): React.JSX.Element {
  return (
    <thead>
      <tr className="border-border border-b">
        {columns.map((col) => (
          <th key={col.key} className="px-2 py-1 text-left text-xs font-medium">
            {col.label}
          </th>
        ))}
        <th className="w-8" />
      </tr>
    </thead>
  )
}

function TableRow({
  row,
  columns,
  disabled,
  onCellChange,
  onRemove,
}: {
  row: Record<string, unknown>
  columns: readonly ObjectArrayColumn[]
  disabled: boolean | undefined
  onCellChange: (column: ObjectArrayColumn, next: unknown) => void
  onRemove: () => void
}): React.JSX.Element {
  return (
    <tr className="border-border/50 border-b last:border-0">
      {columns.map((col) => (
        <td key={col.key} className="px-2 py-1">
          <CellInput
            column={col}
            cellValue={_readDottedCell(row, col.key)}
            disabled={disabled}
            onCellChange={(next) => {
              onCellChange(col, next)
            }}
          />
        </td>
      ))}
      <td className="px-2 py-1 text-right">
        <button
          type="button"
          aria-label="Remove row"
          disabled={disabled}
          onClick={onRemove}
          className="hover:text-destructive cursor-pointer text-sm disabled:cursor-not-allowed disabled:opacity-50"
        >
          ×
        </button>
      </td>
    </tr>
  )
}

function TableToolbar({
  path,
  label,
  disabled,
  onAddRow,
}: {
  path: string
  label: string
  disabled: boolean | undefined
  onAddRow: () => void
}): React.JSX.Element {
  return (
    <div className="flex items-center justify-between">
      <span id={`${path}-label`} className="text-sm font-medium">
        {label}
      </span>
      <button
        type="button"
        disabled={disabled}
        onClick={onAddRow}
        className="bg-primary text-primary-foreground hover:bg-primary/90 h-8 cursor-pointer rounded-md px-3 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50"
      >
        Add row
      </button>
    </div>
  )
}

function TableBody({
  rows,
  columns,
  disabled,
  onCellChange,
  onRemoveRow,
}: {
  rows: readonly Record<string, unknown>[]
  columns: readonly ObjectArrayColumn[]
  disabled: boolean | undefined
  onCellChange: (rowIndex: number, column: ObjectArrayColumn, next: unknown) => void
  onRemoveRow: (rowIndex: number) => void
}): React.JSX.Element {
  return (
    <tbody>
      {rows.map((row, rowIndex) => (
        <TableRow
          // eslint-disable-next-line react/no-array-index-key
          key={rowIndex}
          row={row}
          columns={columns}
          disabled={disabled}
          onCellChange={(col, next) => {
            onCellChange(rowIndex, col, next)
          }}
          onRemove={() => {
            onRemoveRow(rowIndex)
          }}
        />
      ))}
    </tbody>
  )
}

export function ObjectArrayTableEditor({
  path,
  label,
  value,
  columns,
  onChange,
  disabled,
  className,
}: ObjectArrayTableEditorProps): React.JSX.Element {
  const handleAddRow = (): void => {
    onChange([...value, emptyRow(columns)])
  }
  const handleRemoveRow = (rowIndex: number): void => {
    onChange(value.filter((_, idx) => idx !== rowIndex))
  }
  const handleCellChange = (rowIndex: number, column: ObjectArrayColumn, next: unknown): void => {
    onChange(
      value.map((row, idx) => (idx === rowIndex ? _writeDottedCell(row, column.key, next) : row)),
    )
  }
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <TableToolbar path={path} label={label} disabled={disabled} onAddRow={handleAddRow} />
      <table aria-labelledby={`${path}-label`} className="w-full text-sm">
        <TableHeader columns={columns} />
        <TableBody
          rows={value}
          columns={columns}
          disabled={disabled}
          onCellChange={handleCellChange}
          onRemoveRow={handleRemoveRow}
        />
      </table>
    </div>
  )
}
