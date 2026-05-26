// Vitest coverage for ObjectArrayTableEditor's dotted-path columns
// (Wave-6 finding #12).
//
// Pre-fix the editor only supported flat row shapes — ``row[col.key]`` was
// the read path. When the backend started emitting ``dict[str, BaseModel]``
// columns as ``value.<field>`` paths, the editor needed to descend into the
// nested object on read AND clone-write on update.

import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import {
  type ObjectArrayColumn,
  ObjectArrayTableEditor,
} from '@/views/configuration/framework/object-array-table-editor'

const COLUMNS: readonly ObjectArrayColumn[] = [
  { key: 'key', label: 'Agent', controlType: 'string' },
  { key: 'value.context_lower', label: 'ctx lower', controlType: 'number' },
  { key: 'value.context_upper', label: 'ctx upper', controlType: 'number' },
]

const SAMPLE_ROW = {
  key: 'analyst',
  value: { context_lower: 32_000, context_upper: 64_000 },
} as const

function _renderEditor(
  rows: readonly Record<string, unknown>[],
  onChange: (rows: Record<string, unknown>[]) => void,
): void {
  render(
    <ObjectArrayTableEditor
      path="budgets"
      label="Budgets"
      value={rows}
      columns={COLUMNS}
      onChange={onChange}
    />,
  )
}

function _noopChange(_rows: Record<string, unknown>[]): void {
  // No-op onChange handler for read-only render tests.
}

describe('ObjectArrayTableEditor — dotted-path cells', () => {
  it('reads nested cell values via dotted paths', () => {
    _renderEditor([SAMPLE_ROW], _noopChange)
    expect(screen.getByDisplayValue('analyst')).toBeInTheDocument()
    expect(screen.getByDisplayValue(32_000)).toBeInTheDocument()
    expect(screen.getByDisplayValue(64_000)).toBeInTheDocument()
  })

  it('writes nested cell values via dotted paths without trampling siblings', () => {
    // ``fireEvent.change`` sets the input value atomically and triggers a
    // single onChange — easier to assert on than ``user.type``, which
    // dispatches a keystroke per character (each keystroke produces an
    // intermediate onChange that doesn't yet reflect the final value).
    const onChange = vi.fn()
    _renderEditor([SAMPLE_ROW], onChange)
    const lowerInput = screen.getByDisplayValue(32_000)
    fireEvent.change(lowerInput, { target: { value: '40000' } })
    expect(onChange).toHaveBeenCalled()
    const lastCall = onChange.mock.calls.at(-1)
    expect(lastCall).toBeDefined()
    if (lastCall === undefined) {
      return
    }
    const rows = lastCall[0] as Record<string, unknown>[]
    expect(rows).toHaveLength(1)
    const updated = (rows[0] as { value: Record<string, unknown> }).value
    expect(updated.context_lower).toBe(40_000)
    // The sibling field must NOT be wiped by the descent-write.
    expect(updated.context_upper).toBe(64_000)
  })

  it('initialises empty rows with the dotted-path shape', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    _renderEditor([], onChange)
    await user.click(screen.getByRole('button', { name: /add row/i }))
    expect(onChange).toHaveBeenCalled()
    const rows = onChange.mock.calls[0][0] as Record<string, unknown>[]
    expect(rows).toHaveLength(1)
    const row = rows[0] as { key: string; value: Record<string, unknown> }
    expect(row.key).toBe('')
    expect(row.value).toEqual({ context_lower: 0, context_upper: 0 })
  })
})
