// Vitest tests for the config editor framework controls (ALP-679).
//
// Each per-control-type React component renders its label, current value,
// and dispatches a change event when the operator edits the value. The
// FormComposer (separate test) dispatches across these per the form
// schema's control_type.

import { useState } from 'react'

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { BooleanToggle } from '@/views/configuration/framework/boolean-toggle'
import { EnumDropdown } from '@/views/configuration/framework/enum-dropdown'
import { NumberInput } from '@/views/configuration/framework/number-input'
import { StringArrayTagEditor } from '@/views/configuration/framework/string-array-tag-editor'
import { StringInput } from '@/views/configuration/framework/string-input'

// ── Controlled-component wrappers ─────────────────────────────────────────────
//
// The controls are controlled — value is owned by the parent. Tests that
// drive multiple keystrokes need a stateful wrapper so the input's value
// updates between strokes. The wrapper forwards each change to the spy
// while owning the per-render `value` for the inner control.

function ControlledNumberInput({
  initial,
  onChange,
}: {
  initial: number
  onChange: (next: number) => void
}): React.JSX.Element {
  const [value, setValue] = useState(initial)
  return (
    <NumberInput
      path="bind.port"
      label="Port"
      value={value}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
      constraints={{ minimum: 1, maximum: 65_535 }}
    />
  )
}

function ControlledStringInput({
  initial,
  onChange,
}: {
  initial: string
  onChange: (next: string) => void
}): React.JSX.Element {
  const [value, setValue] = useState(initial)
  return (
    <StringInput
      path="x"
      label="Host"
      value={value}
      onChange={(next) => {
        setValue(next)
        onChange(next)
      }}
    />
  )
}

// ── NumberInput ───────────────────────────────────────────────────────────────

describe('NumberInput', () => {
  it('renders the field label and current value', () => {
    render(
      <NumberInput
        path="bind.port"
        label="Port"
        value={8080}
        onChange={vi.fn()}
        constraints={{ minimum: 1, maximum: 65_535 }}
      />,
    )
    expect(screen.getByLabelText('Port')).toHaveValue(8080)
  })

  it('dispatches the new numeric value on change', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    // Controlled wrapper: state lives in the parent so user.clear actually
    // empties the input before typing.
    render(<ControlledNumberInput initial={8080} onChange={onChange} />)
    const input = screen.getByLabelText('Port')
    await user.clear(input)
    await user.type(input, '9090')
    expect(onChange).toHaveBeenLastCalledWith(9090)
  })

  it('renders the unit suffix when provided', () => {
    render(
      <NumberInput
        path="x"
        label="Duration"
        value={12}
        onChange={vi.fn()}
        constraints={{ minimum: 1 }}
        unit="hours"
      />,
    )
    expect(screen.getByText('hours')).toBeInTheDocument()
  })

  it('reflects min and max constraints on the underlying input', () => {
    render(
      <NumberInput
        path="x"
        label="Range"
        value={5}
        onChange={vi.fn()}
        constraints={{ minimum: 1, maximum: 10 }}
      />,
    )
    const input = screen.getByLabelText('Range')
    expect(input).toHaveAttribute('min', '1')
    expect(input).toHaveAttribute('max', '10')
  })
})

// ── BooleanToggle ─────────────────────────────────────────────────────────────

describe('BooleanToggle', () => {
  it('renders true / false toggle state visibly', () => {
    render(<BooleanToggle path="x" label="Enabled" value onChange={vi.fn()} />)
    expect(screen.getByLabelText('Enabled')).toBeChecked()
  })

  it('dispatches the inverted value on click', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<BooleanToggle path="x" label="Enabled" value={false} onChange={onChange} />)
    await user.click(screen.getByLabelText('Enabled'))
    expect(onChange).toHaveBeenCalledWith(true)
  })
})

// ── EnumDropdown ──────────────────────────────────────────────────────────────

describe('EnumDropdown', () => {
  it('renders each choice as a selectable option', () => {
    render(
      <EnumDropdown
        path="severity"
        label="Severity"
        value="critical"
        onChange={vi.fn()}
        choices={['critical', 'important', 'operational']}
      />,
    )
    expect(screen.getByLabelText('Severity')).toHaveValue('critical')
    expect(screen.getByRole('option', { name: 'critical' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'important' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'operational' })).toBeInTheDocument()
  })

  it('dispatches the chosen value on selection', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <EnumDropdown
        path="severity"
        label="Severity"
        value="critical"
        onChange={onChange}
        choices={['critical', 'important', 'operational']}
      />,
    )
    await user.selectOptions(screen.getByLabelText('Severity'), 'important')
    expect(onChange).toHaveBeenCalledWith('important')
  })
})

// ── StringInput ───────────────────────────────────────────────────────────────

describe('StringInput', () => {
  it('renders the current string value', () => {
    render(<StringInput path="x" label="Host" value="127.0.0.1" onChange={vi.fn()} />)
    expect(screen.getByLabelText('Host')).toHaveValue('127.0.0.1')
  })

  it('dispatches the new string value on change', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<ControlledStringInput initial="" onChange={onChange} />)
    await user.type(screen.getByLabelText('Host'), 'abc')
    expect(onChange).toHaveBeenLastCalledWith('abc')
  })

  it('renders a regex validation hint when pattern is provided', () => {
    render(
      <StringInput
        path="x"
        label="Ticker"
        value="bad-ticker"
        onChange={vi.fn()}
        constraints={{ pattern: '^[A-Z]+$' }}
      />,
    )
    // The pattern attribute lands on the input so HTML5 + tests see it.
    expect(screen.getByLabelText('Ticker')).toHaveAttribute('pattern', '^[A-Z]+$')
  })
})

// ── StringArrayTagEditor ──────────────────────────────────────────────────────

describe('StringArrayTagEditor', () => {
  it('renders each existing tag as a chip', () => {
    render(
      <StringArrayTagEditor
        path="x"
        label="Active sectors"
        value={['tech', 'semis', 'financials']}
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByText('tech')).toBeInTheDocument()
    expect(screen.getByText('semis')).toBeInTheDocument()
    expect(screen.getByText('financials')).toBeInTheDocument()
  })

  it('appends a new tag when the add button is pressed', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <StringArrayTagEditor path="x" label="Active sectors" value={['tech']} onChange={onChange} />,
    )
    await user.type(screen.getByPlaceholderText(/add/i), 'energy')
    await user.click(screen.getByRole('button', { name: /add/i }))
    expect(onChange).toHaveBeenCalledWith(['tech', 'energy'])
  })

  it('removes a tag when its remove button is pressed', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(
      <StringArrayTagEditor
        path="x"
        label="Active sectors"
        value={['tech', 'semis']}
        onChange={onChange}
      />,
    )
    await user.click(screen.getByLabelText(/remove tech/i))
    expect(onChange).toHaveBeenCalledWith(['semis'])
  })

  it('does not append an empty tag', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<StringArrayTagEditor path="x" label="x" value={['tech']} onChange={onChange} />)
    await user.click(screen.getByRole('button', { name: /add/i }))
    expect(onChange).not.toHaveBeenCalled()
  })
})
