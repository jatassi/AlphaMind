// Vitest coverage for src/lib/yaml-dump (Wave-6 fix).
//
// Regression guard: pre-fix the helper emitted JSON via
// ``JSON.stringify`` despite the name, so every profile/regime save
// flattened the on-disk YAML to JSON shape (operators reading
// ``git diff`` on the YAML saw a wholesale rewrite, the git-history
// viewer's diff was meaningless).  These tests pin the helper to real
// YAML emission via js-yaml.

import * as yaml from 'js-yaml'
import { describe, expect, it } from 'vitest'

import dump from '@/lib/yaml-dump'

describe('yaml-dump', () => {
  it('emits real YAML, not JSON', () => {
    const value = { foo: 'bar', baz: [1, 2, 3] }
    const text = dump(value)
    expect(text).not.toMatch(/^{/)
    expect(text).toContain('foo: bar')
    expect(text).toContain('baz:')
  })

  it('round-trips through js-yaml safeLoad', () => {
    const value = {
      risk_priority: 'signal_quality',
      min_position_size_usd: 1234,
      rule_values: { breach_window_minutes: 30 },
    }
    const text = dump(value)
    expect(yaml.load(text)).toEqual(value)
  })

  it('does not emit YAML anchors / aliases', () => {
    // ``noRefs: true`` should suppress the ``&anchor`` / ``*alias`` shape
    // even when the input has structurally repeated subtrees — operators
    // editing a profile shouldn't see surprise anchor markers.
    const shared = { ratio: 0.5 }
    const value = { a: shared, b: shared }
    const text = dump(value)
    expect(text).not.toContain('&')
    expect(text).not.toContain('*')
  })
})
