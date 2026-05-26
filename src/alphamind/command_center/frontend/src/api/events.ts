import { useEffect, useRef } from 'react'

// useEventStream — React hook wrapping the EventSource against `/api/events`
// (story 04b's SSE multiplexer). Subscribers register interest by
// `<source>:<event_name>` (e.g. `pipeline:invocation_started`); the hook
// fans events to matching subscribers and reconnects with exponential
// backoff on close.
//
// Slow-consumer protection: per-subscriber queue capped at a small N;
// overflow drops the oldest event and surfaces a one-time warning via the
// optional `onOverflow` callback (view stories wire this to a toast).
//
// SSE wire shape: the backend (`command_center/events/routes.py`) emits
// each frame with a NAMED ``event:`` line — ``pipeline:<event_name>``,
// ``monitor:<event_name>``, or the unqualified ``heartbeat`` — and the
// ``data:`` line is a JSON envelope whose ``event`` field carries the
// upstream event name (e.g. ``invocation_started``). Browser
// ``EventSource`` fires the ``'message'`` listener ONLY for unnamed
// frames; for named frames we must register one listener per event-name
// the server emits (F1). The envelope's JSON field is ``event`` (not
// ``event_name``) — that mismatch is F2.

const RECONNECT_INITIAL_MS = 500
const RECONNECT_MAX_MS = 30_000
const DEFAULT_QUEUE_CAP = 100

// Wire-event names the backend emits as the SSE ``event:`` field.
// Pipeline + monitor frames carry the ``<source>:<event_name>`` shape; the
// backend's own keep-alive carries the unqualified ``heartbeat`` shape.
// See ``alphamind.command_center._kernel.events`` for the StrEnum sources
// of truth.

const PIPELINE_EVENT_NAMES = [
  'invocation_started',
  'phase_transition',
  'agent_started',
  'agent_succeeded',
  'agent_retrying',
  'agent_failed',
  'invocation_ended',
  'next_trigger_changed',
  'heartbeat',
] as const

const MONITOR_EVENT_NAMES = [
  'websocket_connected',
  'websocket_disconnected',
  'fill_received',
  'breach_detected',
  'emergency_invocation_triggered',
  'greeks_refreshed',
  'heartbeat',
] as const

// Command-center-sourced events — the InAppChannel publishes
// AlertFiredEvent onto the multiplexer, and ``events/routes.py`` emits
// them with ``event: cc:alert_fired``. Without a listener registered
// here the browser EventSource would never receive the frame and the
// in-app alerts banner would silently stay empty (finding #8,
// Wave-5 review).
const CC_EVENT_NAMES = ['alert_fired'] as const

const BACKEND_HEARTBEAT_EVENT_NAME = 'heartbeat'

// All SSE ``event:`` field values the backend emits. Iteration target for
// ``addEventListener`` registration (F1).
export const SSE_EVENT_NAMES: readonly string[] = [
  ...PIPELINE_EVENT_NAMES.map((name) => `pipeline:${name}`),
  ...MONITOR_EVENT_NAMES.map((name) => `monitor:${name}`),
  ...CC_EVENT_NAMES.map((name) => `cc:${name}`),
  BACKEND_HEARTBEAT_EVENT_NAME,
]

export type EventStreamMessage = {
  source: string
  event_name: string
  payload: unknown
}

export type EventStreamSubscriber = (message: EventStreamMessage) => void

export type UseEventStreamOptions = {
  // Map of `<source>:<event_name>` → subscriber callback. Use `*:*` to
  // receive every event. `pipeline:*` matches every event from the
  // pipeline source. `*:invocation_started` matches that event regardless
  // of source.
  subscribers: Record<string, EventStreamSubscriber>
  // Optional toast hook fired when a subscriber's queue overflows.
  onOverflow?: (key: string) => void
  // Override the SSE endpoint for tests. Defaults to '/api/events'.
  url?: string
  // Override the per-subscriber queue cap. Defaults to DEFAULT_QUEUE_CAP.
  queueCap?: number
}

type DispatchContext = {
  subscribers: Record<string, EventStreamSubscriber>
  queues: Map<string, EventStreamMessage[]>
  queueCap: number
  onOverflow: ((key: string) => void) | undefined
}

function matchesSubscription(key: string, source: string, eventName: string): boolean {
  const colonIndex = key.indexOf(':')
  if (colonIndex === -1) {
    return false
  }
  const keySource = key.slice(0, colonIndex)
  const keyEvent = key.slice(colonIndex + 1)
  const sourceMatch = keySource === '*' || keySource === source
  const eventMatch = keyEvent === '*' || keyEvent === eventName
  return sourceMatch && eventMatch
}

// Backend's own keep-alive frame: SSE ``event: heartbeat`` (unqualified)
// with a JSON envelope that does NOT carry source/event fields. We surface
// these to subscribers under the synthetic ``backend`` source so views can
// distinguish "command-center alive" from "upstream alive".
const BACKEND_SOURCE = 'backend'

function parseUpstreamMessage(rawData: string): EventStreamMessage | null {
  try {
    const parsed: unknown = JSON.parse(rawData)
    if (typeof parsed !== 'object' || parsed === null) {
      return null
    }
    const obj = parsed as Record<string, unknown>
    // The envelope's wire field is ``event`` (F2 — was previously read as
    // ``event_name`` which never matched the server's serialization). The
    // ``source`` and ``data`` fields are likewise the wire names.
    const source = obj.source
    const eventName = obj.event
    if (typeof source !== 'string' || typeof eventName !== 'string') {
      return null
    }
    return {
      source,
      event_name: eventName,
      payload: obj.data,
    }
  } catch {
    return null
  }
}

function parseBackendHeartbeat(rawData: string): EventStreamMessage | null {
  // The backend's keep-alive carries only ``{"timestamp": "..."}``; surface
  // it as a synthetic ``backend:heartbeat`` message so view layers can
  // subscribe.
  try {
    const parsed: unknown = JSON.parse(rawData)
    if (typeof parsed !== 'object' || parsed === null) {
      return null
    }
    return {
      source: BACKEND_SOURCE,
      event_name: BACKEND_HEARTBEAT_EVENT_NAME,
      payload: parsed,
    }
  } catch {
    return null
  }
}

function dispatchToSubscribers(message: EventStreamMessage, ctx: DispatchContext): void {
  for (const [key, subscriber] of Object.entries(ctx.subscribers)) {
    if (!matchesSubscription(key, message.source, message.event_name)) {
      continue
    }
    let queue = ctx.queues.get(key)
    if (queue === undefined) {
      queue = []
      ctx.queues.set(key, queue)
    }
    if (queue.length >= ctx.queueCap) {
      queue.shift()
      ctx.onOverflow?.(key)
    }
    queue.push(message)
    try {
      subscriber(message)
    } catch {
      // A subscriber's bug must not kill the whole stream — swallow it.
    }
  }
}

type ConnectionState = {
  closed: boolean
  source: EventSource | null
  reconnectDelay: number
  reconnectTimer: ReturnType<typeof setTimeout> | null
}

type BindContext = {
  source: EventSource
  state: ConnectionState
  buildCtx: () => DispatchContext
  reconnect: () => void
}

function handleNamedEvent(e: MessageEvent<string>, ctx: DispatchContext): void {
  const message = parseUpstreamMessage(e.data)
  if (message === null) {
    return
  }
  dispatchToSubscribers(message, ctx)
}

function handleBackendHeartbeat(e: MessageEvent<string>, ctx: DispatchContext): void {
  const message = parseBackendHeartbeat(e.data)
  if (message === null) {
    return
  }
  dispatchToSubscribers(message, ctx)
}

function bindSource(ctx: BindContext): void {
  const { source, state, buildCtx, reconnect } = ctx
  source.addEventListener('open', () => {
    state.reconnectDelay = RECONNECT_INITIAL_MS
  })
  // Register one listener per server-emitted event name (F1). Browser
  // ``EventSource`` dispatches the listener whose name matches the
  // frame's ``event:`` field exactly; the catch-all ``'message'``
  // listener fires only on unnamed frames (which our backend never
  // emits), so a single ``'message'`` registration would receive zero
  // frames. The unqualified ``heartbeat`` frame uses the backend-source
  // parser; everything else uses the upstream envelope parser.
  for (const name of SSE_EVENT_NAMES) {
    const handler = (e: Event): void => {
      const messageEvent = e as MessageEvent<string>
      if (name === BACKEND_HEARTBEAT_EVENT_NAME) {
        handleBackendHeartbeat(messageEvent, buildCtx())
      } else {
        handleNamedEvent(messageEvent, buildCtx())
      }
    }
    source.addEventListener(name, handler)
  }
  source.addEventListener('error', () => {
    state.source?.close()
    state.source = null
    if (state.closed) {
      return
    }
    state.reconnectTimer = setTimeout(reconnect, state.reconnectDelay)
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, RECONNECT_MAX_MS)
  })
}

export function useEventStream(options: UseEventStreamOptions): void {
  const { subscribers, onOverflow, url = '/api/events', queueCap = DEFAULT_QUEUE_CAP } = options
  const subscribersRef = useRef(subscribers)
  useEffect(() => {
    subscribersRef.current = subscribers
  }, [subscribers])

  useEffect(() => {
    if (typeof EventSource === 'undefined') {
      // SSR / non-browser environment — no-op.
      return
    }
    const queues = new Map<string, EventStreamMessage[]>()
    const state: ConnectionState = {
      closed: false,
      source: null,
      reconnectDelay: RECONNECT_INITIAL_MS,
      reconnectTimer: null,
    }
    const buildCtx = (): DispatchContext => ({
      subscribers: subscribersRef.current,
      queues,
      queueCap,
      onOverflow,
    })
    const connect = (): void => {
      if (state.closed) {
        return
      }
      state.source = new EventSource(url)
      bindSource({ source: state.source, state, buildCtx, reconnect: connect })
    }
    connect()
    return () => {
      state.closed = true
      if (state.reconnectTimer !== null) {
        clearTimeout(state.reconnectTimer)
      }
      state.source?.close()
    }
  }, [url, queueCap, onOverflow])
}
