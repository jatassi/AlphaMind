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
// Story 04b ships the server side of /api/events; until that lands the
// hook is wired but won't receive events. The shape is locked here so
// view stories can subscribe against a stable API.

const RECONNECT_INITIAL_MS = 500
const RECONNECT_MAX_MS = 30_000
const DEFAULT_QUEUE_CAP = 100

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

function parseMessage(rawData: string): EventStreamMessage | null {
  try {
    const parsed: unknown = JSON.parse(rawData)
    if (typeof parsed !== 'object' || parsed === null) {
      return null
    }
    const obj = parsed as Record<string, unknown>
    if (typeof obj.source !== 'string' || typeof obj.event_name !== 'string') {
      return null
    }
    return {
      source: obj.source,
      event_name: obj.event_name,
      payload: obj.payload,
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

function bindSource(ctx: BindContext): void {
  const { source, state, buildCtx, reconnect } = ctx
  source.addEventListener('open', () => {
    state.reconnectDelay = RECONNECT_INITIAL_MS
  })
  source.addEventListener('message', (e) => {
    const message = parseMessage((e as MessageEvent<string>).data)
    if (message === null) {
      return
    }
    dispatchToSubscribers(message, buildCtx())
  })
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
