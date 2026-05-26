// Vitest tests for the second batch of config editor framework controls
// (ALP-679): ObjectArrayTableEditor, CronExpressionPicker, PathInput.

import { useState } from 'react'

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { CronExpressionPicker } from '@/views/configuration/framework/cron-expression-picker'
import {
  type ObjectArrayColumn,
  ObjectArrayTableEditor,
} from '@/views/configuration/framework/object-array-table-editor'
import { PathInput } from '@/views/configuration/framework/path-input'

// ── ObjectArrayTableEditor ────────────────────────────────────────────────────

const RULE_COLUMNS: readonly ObjectArrayColumn[] = [
  { key: 'name', label: 'Name', controlType: 'string' },
  { key: 'severity', label: 'Severity', controlType: 'string' },
  { key: 'debounce_minutes', label: 'Debounce', controlType: 'number' },
]

function ControlledObjectArrayTableEditor({
  initial,
  onChange,
}: {
  initial: readonly Record<string, unknown>[]
  onChange: (next: Record<string, unknown>[]) => void
}): React.JSX.Element {
  const [rows, setRows] = useState<readonly Record<string, unknown>[]>(initial)
  return (
    <ObjectArrayTableEditor
      path="rules"
      label="Alert rules"
      value={rows}
      columns={RULE_COLUMNS}
      onChange={(next) => {
        setRows(next)
        onChange(next)
      }}
    />
  )
}

describe('ObjectArrayTableEditor renders', () => {
  it('renders one row per object with one cell per column', () => {
    render(
      <ObjectArrayTableEditor
        path="rules"
        label="Alert rules"
        value={[{ name: 'pipeline_aborted', severity: 'critical', debounce_minutes: 15 }]}
        columns={RULE_COLUMNS}
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByDisplayValue('pipeline_aborted')).toBeInTheDocument()
    expect(screen.getByDisplayValue('critical')).toBeInTheDocument()
    expect(screen.getByDisplayValue('15')).toBeInTheDocument()
  })

  it('renders column headers', () => {
    render(
      <ObjectArrayTableEditor
        path="rules"
        label="Alert rules"
        value={[]}
        columns={RULE_COLUMNS}
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByText('Name')).toBeInTheDocument()
    expect(screen.getByText('Severity')).toBeInTheDocument()
    expect(screen.getByText('Debounce')).toBeInTheDocument()
  })
})

describe('ObjectArrayTableEditor row add/remove', () => {
  it('appends an empty row when the add-row button is clicked', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ControlledObjectArrayTableEditor
        initial={[{ name: 'a', severity: 's', debounce_minutes: 10 }]}
        onChange={onChange}
      />,
    )
    await user.click(screen.getByRole('button', { name: /add row/i }))
    expect(onChange).toHaveBeenCalled()
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>[]
    expect(last).toHaveLength(2)
    expect(last[1]).toEqual({ name: '', severity: '', debounce_minutes: 0 })
  })

  it('removes a row when its remove button is clicked', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ControlledObjectArrayTableEditor
        initial={[
          { name: 'a', severity: 's', debounce_minutes: 10 },
          { name: 'b', severity: 'c', debounce_minutes: 20 },
        ]}
        onChange={onChange}
      />,
    )
    const buttons = screen.getAllByRole('button', { name: /remove row/i })
    await user.click(buttons[0])
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>[]
    expect(last).toHaveLength(1)
    expect(last[0].name).toBe('b')
  })
})

describe('ObjectArrayTableEditor cell edits', () => {
  it('dispatches a cell edit on the change event', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <ControlledObjectArrayTableEditor
        initial={[{ name: 'a', severity: 's', debounce_minutes: 10 }]}
        onChange={onChange}
      />,
    )
    const nameInput = screen.getByDisplayValue('a')
    await user.clear(nameInput)
    await user.type(nameInput, 'new_name')
    const last = onChange.mock.calls.at(-1)?.[0] as Record<string, unknown>[]
    expect(last[0].name).toBe('new_name')
  })
})

// ── CronExpressionPicker ──────────────────────────────────────────────────────

function ControlledCronExpressionPicker({
  initial,
  onChange,
}: {
  initial: string
  onChange: (next: string) => void
}): React.JSX.Element {
  const [value, setValue] = useState(initial)
  return (
    <CronExpressionPicker
      path="cron"
      label="Schedule"
      value={value}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

describe('CronExpressionPicker', () => {
  it('shows the underlying cron string read-only', () => {
    render(
      <CronExpressionPicker
        path="cron"
        label="Schedule"
        value="30 9,11,13,15 * * mon-fri"
        onChange={vi.fn()}
      />,
    )
    // Read-only cron preview is visible somewhere on the surface.
    expect(screen.getByText('30 9,11,13,15 * * mon-fri')).toBeInTheDocument()
  })

  it('renders structured every-N-hours + HH:MM inputs', () => {
    render(
      <CronExpressionPicker
        path="cron"
        label="Schedule"
        value="0 0,4,8 * * mon-fri"
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByLabelText(/every n hours/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/anchor minute/i)).toBeInTheDocument()
  })

  it('updates the cron string when the structured inputs change', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<ControlledCronExpressionPicker initial="0 0,4,8 * * mon-fri" onChange={onChange} />)
    const hoursInput = screen.getByLabelText(/every n hours/i)
    await user.clear(hoursInput)
    await user.type(hoursInput, '6')
    // The cron string update fires onChange with the recomposed expression.
    expect(onChange).toHaveBeenCalled()
    const last = onChange.mock.calls.at(-1)?.[0] as string
    // 6-hour cadence at minute 0 over 24 hours: 0,6,12,18.
    expect(last).toContain('0,6,12,18')
  })
})

// ── PathInput ─────────────────────────────────────────────────────────────────

describe('PathInput', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the current path value', () => {
    render(<PathInput path="x" label="DB path" value="/data/alphamind.db" onChange={vi.fn()} />)
    expect(screen.getByLabelText('DB path')).toHaveValue('/data/alphamind.db')
  })

  it('renders the existence indicator when the probe reports true', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ exists: true }),
      }),
    )
    render(<PathInput path="x" label="DB path" value="/data/alphamind.db" onChange={vi.fn()} />)
    expect(await screen.findByText(/exists/i)).toBeInTheDocument()
  })

  it('renders the missing indicator when the probe reports false', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ exists: false }),
      }),
    )
    render(<PathInput path="x" label="DB path" value="/does-not-exist" onChange={vi.fn()} />)
    expect(await screen.findByText(/missing/i)).toBeInTheDocument()
  })
})
