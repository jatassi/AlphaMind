import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query'

import { api, ApiError } from './client'

// TanStack Query hook factories. View stories add their own per-endpoint
// hooks here; the foundation ships `useSession()`, which the protected
// `_authed` route layout calls to gate access to the views.
//
// Query keys are arrays whose first element is the conceptual scope
// (`['session']`, `['runs']`, …) so per-view cache invalidation can target
// the right slice without leaking each view's URL shape into siblings.

export type SessionInfo = {
  session_id: string
  expires_at: string
}

const SESSION_QUERY_KEY = ['session'] as const

// Detail the /auth/me endpoint exposes — the resolved session id + expiry.
// The session row itself stays server-side; the frontend only needs to know
// "am I authenticated?" plus a freshness window for cache invalidation.
export function sessionQueryOptions(): ReturnType<typeof queryOptions<SessionInfo | null>> {
  return queryOptions<SessionInfo | null>({
    queryKey: SESSION_QUERY_KEY,
    queryFn: async () => {
      try {
        return await api.get<SessionInfo>('/auth/me')
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          return null
        }
        throw error
      }
    },
    // Retry once on transient network errors; refetch is cheap so we don't
    // need an aggressive stale time for v1. Per-view stories can override
    // their own queries' freshness if they need real-time semantics.
    retry: 1,
    staleTime: 30 * 1000,
  })
}

export function useSession(): UseQueryResult<SessionInfo | null> {
  return useQuery(sessionQueryOptions())
}
