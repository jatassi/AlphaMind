// Typed fetch wrapper for the command-center HTTP surface.
//
// Responsibilities:
//
// * Inject `X-CSRF-Token` on mutating verbs (POST / PUT / PATCH / DELETE).
//   The token lives in the `cc_csrf` cookie (non-HttpOnly per story 03);
//   we read it via `document.cookie` and echo it on the header. The auth
//   layer's CSRF dependency does the double-submit comparison.
// * On 401, dispatch an `auth:unauthorized` event and redirect to /login
//   so the active hook tree can react before navigation.
// * Throw a typed `ApiError` on non-2xx so callers can branch by status.
//
// This wrapper is the only path through which view code talks to FastAPI;
// per-view stories build TanStack Query hook factories on top of it.

const MUTATING_VERBS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])
const CSRF_COOKIE_NAME = 'cc_csrf'

export class ApiError extends Error {
  readonly status: number
  readonly detail: unknown

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? `API request failed with status ${String(status)}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

// Read a cookie value by name from `document.cookie`. Returns null if the
// cookie isn't set. Cookie parsing is intentionally minimal — the only
// cookie we read here is `cc_csrf`, which is set by the backend with no
// special characters in the value.
export function readCookie(name: string): string | null {
  if (typeof document === 'undefined') {
    return null
  }
  const entries = document.cookie.split(';')
  for (const entry of entries) {
    const equalsIndex = entry.indexOf('=')
    if (equalsIndex === -1) {
      continue
    }
    const key = entry.slice(0, equalsIndex).trim()
    if (key === name) {
      return entry.slice(equalsIndex + 1).trim()
    }
  }
  return null
}

function buildHeaders(method: string, init: RequestInit | undefined): Headers {
  const headers = new Headers(init?.headers)
  if (!headers.has('Content-Type') && init?.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }
  if (MUTATING_VERBS.has(method.toUpperCase())) {
    const csrf = readCookie(CSRF_COOKIE_NAME)
    if (csrf !== null) {
      headers.set('X-CSRF-Token', csrf)
    }
  }
  return headers
}

function handleUnauthorized(): void {
  if (typeof globalThis.dispatchEvent === 'function') {
    globalThis.dispatchEvent(new CustomEvent('auth:unauthorized'))
  }
  // Skip redirect in non-browser environments (jsdom-less tests) or when
  // already on /login (avoids redirect loop on the login page's own 401s).
  if (typeof globalThis.location === 'object' && globalThis.location.pathname !== '/login') {
    globalThis.location.assign('/login')
  }
}

async function parseErrorBody(res: Response): Promise<unknown> {
  try {
    return (await res.json()) as unknown
  } catch {
    return null
  }
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase()
  const headers = buildHeaders(method, init)
  const res = await fetch(path, {
    ...init,
    method,
    headers,
    credentials: 'same-origin',
  })
  if (!res.ok) {
    if (res.status === 401) {
      handleUnauthorized()
    }
    const detail = await parseErrorBody(res)
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) {
    return undefined as T
  }
  return (await res.json()) as T
}

// Convenience helpers — one per verb. View stories use these directly
// from their TanStack Query hooks.
export const api = {
  get: <T>(path: string, init?: RequestInit) => apiFetch<T>(path, { ...init, method: 'GET' }),
  post: <T>(path: string, body?: unknown, init?: RequestInit) =>
    apiFetch<T>(path, {
      ...init,
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  put: <T>(path: string, body?: unknown, init?: RequestInit) =>
    apiFetch<T>(path, {
      ...init,
      method: 'PUT',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  patch: <T>(path: string, body?: unknown, init?: RequestInit) =>
    apiFetch<T>(path, {
      ...init,
      method: 'PATCH',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  delete: <T>(path: string, init?: RequestInit) => apiFetch<T>(path, { ...init, method: 'DELETE' }),
}
