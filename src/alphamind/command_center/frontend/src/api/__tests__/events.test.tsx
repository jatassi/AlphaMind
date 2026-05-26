import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type EventStreamMessage, SSE_EVENT_NAMES, useEventStream } from '../events'

// Vitest unit tests for the SSE pipeline hook.
//
// Wave-4 findings F1 + F2 fixed a dead listener path: the previous code
// registered a single ``'message'`` listener (which never fires for named
// SSE frames) and parsed ``event_name`` from the envelope (whose actual
// JSON field is ``event``). These tests assert each known event name reaches
// subscribers via the ``addEventListener`` per-name path, and that the
// envelope parser reads the ``event`` field.

// ---------------------------------------------------------------------------
// EventSource stub
// ---------------------------------------------------------------------------
//
// jsdom does not ship EventSource. We replace ``globalThis.EventSource`` with
// a minimal stub that records added listeners; tests drive listeners
// directly via the stub's ``fire(name, data)`` method.

type Handler = (event: { data: string }) => void

class FakeEventSource {
  static instances: FakeEventSource[] = []
  url: string
  listeners = new Map<string, Handler[]>()
  closed = false

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  addEventListener(name: string, handler: Handler): void {
    const list = this.listeners.get(name) ?? []
    list.push(handler)
    this.listeners.set(name, list)
  }

  close(): void {
    this.closed = true
  }

  fire(name: string, data: string): void {
    const list = this.listeners.get(name) ?? []
    for (const handler of list) {
      handler({ data })
    }
  }
}

function setupFakeEventSource(): typeof FakeEventSource {
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  return FakeEventSource
}

// Wrapper component renders the hook so React can mount it under
// ``@testing-library/react``'s ``render``.
function Harness({
  subscribers,
}: {
  subscribers: Record<string, (msg: EventStreamMessage) => void>
}): null {
  useEventStream({ subscribers })
  return null
}

describe('useEventStream wiring', () => {
  beforeEach(() => {
    setupFakeEventSource()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('registers a listener per known event name (F1)', () => {
    render(<Harness subscribers={{}} />)
    const [source] = FakeEventSource.instances
    expect(source).toBeDefined()
    for (const name of SSE_EVENT_NAMES) {
      expect(source.listeners.has(name)).toBe(true)
    }
  })

  it('does NOT rely on the unnamed message listener (F1)', () => {
    render(<Harness subscribers={{}} />)
    const [source] = FakeEventSource.instances
    expect(source.listeners.has('message')).toBe(false)
  })
})

describe('useEventStream envelope parsing', () => {
  beforeEach(() => {
    setupFakeEventSource()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('parses ``event`` not ``event_name`` from the JSON envelope (F2)', () => {
    const received: EventStreamMessage[] = []
    const subscribers = {
      'pipeline:invocation_started': (msg: EventStreamMessage) => received.push(msg),
    }
    render(<Harness subscribers={subscribers} />)
    const [source] = FakeEventSource.instances
    act(() => {
      source.fire(
        'pipeline:invocation_started',
        JSON.stringify({
          source: 'pipeline',
          event: 'invocation_started',
          data: { invocation_id: 'inv-1' },
        }),
      )
    })
    expect(received).toHaveLength(1)
    expect(received[0].source).toBe('pipeline')
    expect(received[0].event_name).toBe('invocation_started')
    expect(received[0].payload).toEqual({ invocation_id: 'inv-1' })
  })

  it('drops envelopes with the legacy ``event_name`` field (F2 reject path)', () => {
    const received: EventStreamMessage[] = []
    const subscribers = {
      '*:*': (msg: EventStreamMessage) => received.push(msg),
    }
    render(<Harness subscribers={subscribers} />)
    const [source] = FakeEventSource.instances
    act(() => {
      source.fire(
        'pipeline:invocation_started',
        JSON.stringify({
          source: 'pipeline',
          event_name: 'invocation_started',
          payload: { invocation_id: 'inv-1' },
        }),
      )
    })
    expect(received).toHaveLength(0)
  })
})

const PER_EVENT_CASES: readonly (readonly [string, string])[] = [
  ['pipeline', 'invocation_started'],
  ['pipeline', 'phase_transition'],
  ['pipeline', 'agent_started'],
  ['pipeline', 'agent_succeeded'],
  ['pipeline', 'agent_retrying'],
  ['pipeline', 'agent_failed'],
  ['pipeline', 'invocation_ended'],
  ['pipeline', 'next_trigger_changed'],
  ['pipeline', 'heartbeat'],
  ['monitor', 'websocket_connected'],
  ['monitor', 'websocket_disconnected'],
  ['monitor', 'fill_received'],
  ['monitor', 'breach_detected'],
  ['monitor', 'emergency_invocation_triggered'],
  ['monitor', 'greeks_refreshed'],
  ['monitor', 'heartbeat'],
]

function assertPerEventRouting(sourceName: string, eventName: string): void {
  const received: EventStreamMessage[] = []
  const subscribers = {
    [`${sourceName}:${eventName}`]: (msg: EventStreamMessage) => received.push(msg),
  }
  render(<Harness subscribers={subscribers} />)
  const [source] = FakeEventSource.instances
  const sseName = `${sourceName}:${eventName}`
  act(() => {
    source.fire(
      sseName,
      JSON.stringify({ source: sourceName, event: eventName, data: { marker: sseName } }),
    )
  })
  expect(received).toHaveLength(1)
  expect(received[0].source).toBe(sourceName)
  expect(received[0].event_name).toBe(eventName)
}

describe('useEventStream per-event dispatch coverage', () => {
  beforeEach(() => {
    setupFakeEventSource()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it.each(PER_EVENT_CASES)('routes %s:%s to a matching subscriber', assertPerEventRouting)

  it('routes the backend keep-alive frame to backend:heartbeat subscribers', () => {
    const received: EventStreamMessage[] = []
    const subscribers = {
      'backend:heartbeat': (msg: EventStreamMessage) => received.push(msg),
    }
    render(<Harness subscribers={subscribers} />)
    const [source] = FakeEventSource.instances
    act(() => {
      source.fire('heartbeat', JSON.stringify({ timestamp: '2026-05-26T00:00:00+00:00' }))
    })
    expect(received).toHaveLength(1)
    expect(received[0].source).toBe('backend')
    expect(received[0].event_name).toBe('heartbeat')
  })
})
