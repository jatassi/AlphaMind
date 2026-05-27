import { useState } from 'react'

import { useMutation } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

import { getAuthenticationCredential, type LoginBeginEnvelope } from './webauthn'

// Login form — single-click WebAuthn ceremony.
//
// One stage: operator clicks "Sign in with passkey". We POST /login/begin
// (which returns the allowed credential list + a challenge), drive
// navigator.credentials.get() against the envelope, then POST
// /login/complete with the assertion. The complete response carries the
// issued session — cookie is set, `useSession()` re-resolves on cache
// invalidation.

export function LoginForm({ onSuccess }: { onSuccess?: () => void }): React.JSX.Element {
  const [error, setError] = useState<string | null>(null)
  const mutation = useMutation({
    mutationFn: async () => {
      const envelope = await api.post<LoginBeginEnvelope>('/auth/login/begin')
      const payload = await getAuthenticationCredential(envelope)
      return api.post('/auth/login/complete', payload)
    },
    onSuccess: () => {
      onSuccess?.()
    },
    onError: (err) => {
      const detail = err instanceof ApiError ? err.detail : null
      setError(typeof detail === 'string' ? detail : err.message)
    },
  })

  return (
    <Card className="w-full max-w-md">
      <CardHeader>
        <CardTitle>Sign in</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {error !== null && (
          <p role="alert" className="text-destructive text-sm">
            {error}
          </p>
        )}
        <Button
          onClick={() => {
            setError(null)
            mutation.mutate()
          }}
          disabled={mutation.isPending}
          className="w-full"
        >
          {mutation.isPending ? 'Authenticating...' : 'Sign in with passkey'}
        </Button>
      </CardContent>
    </Card>
  )
}
