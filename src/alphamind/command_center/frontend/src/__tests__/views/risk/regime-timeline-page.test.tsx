// Vitest coverage for the regime timeline page (ALP-680) — Wave-6 #11.
//
// Pre-fix the page constructed ``setFrom`` and ``setTo`` then
// immediately discarded both with ``void setFrom; void setTo`` —
// the window was pinned to the initial 24h render-time snapshot and
// the operator had no UI to refocus it. The fix wires the setters
// through to two ``<input type="datetime-local">`` controls; this
// test asserts that changing the controls re-fetches with the new
// window.

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RegimeTimelinePage } from '@/views/risk/regime-timeline-page'

function _wrap(ui: React.ReactElement): React.ReactElement {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>
}

beforeEach(() => {
  vi.unstubAllGlobals()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function _emptyTimelineResponse(from: string, to: string): Response {
  return {
    ok: true,
    status: 200,
    json: () =>
      Promise.resolve({
        from_ts: from,
        to_ts: to,
        events: [],
      }),
  } as unknown as Response
}

function _buildResponseFromUrl(url: string): Response {
  const params = new URLSearchParams(url.split('?')[1] ?? '')
  return _emptyTimelineResponse(params.get('from') ?? '', params.get('to') ?? '')
}

function _stubFetchRecording(calls: string[]): ReturnType<typeof vi.fn> {
  return vi.fn((url: string) => {
    calls.push(url)
    return Promise.resolve(_buildResponseFromUrl(url))
  })
}

describe('RegimeTimelinePage', () => {
  it('renders datetime-local controls for the window', async () => {
    vi.stubGlobal('fetch', _stubFetchRecording([]))
    render(_wrap(<RegimeTimelinePage />))
    expect(await screen.findByLabelText(/window start/i)).toBeInTheDocument()
    expect(await screen.findByLabelText(/window end/i)).toBeInTheDocument()
  })

  it('refetches with a new window when the From control changes', async () => {
    const calls: string[] = []
    vi.stubGlobal('fetch', _stubFetchRecording(calls))
    render(_wrap(<RegimeTimelinePage />))
    const start = await screen.findByLabelText(/window start/i)
    const initialCallCount = calls.length
    fireEvent.change(start, { target: { value: '2026-04-01T08:00' } })
    const expectMoreCalls = (): void => {
      expect(calls.length).toBeGreaterThan(initialCallCount)
    }
    await waitFor(expectMoreCalls)
    const expectNewFromTimestamp = (): void => {
      const matches = calls.filter((url) => url.includes('from=2026-04-01T'))
      expect(matches.length).toBeGreaterThan(0)
    }
    await waitFor(expectNewFromTimestamp)
  })
})
