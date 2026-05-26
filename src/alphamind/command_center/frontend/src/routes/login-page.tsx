import { useState } from 'react'

import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'

import { LoginForm } from '@/auth/login'
import { RegisterForm } from '@/auth/register'
import { Button } from '@/components/ui/button'

// Login page — public, no session required. Operators land here when
// unauthenticated; hosts both the login + the registration form behind a
// toggle. First-launch enrollment uses the setup-token field on the
// registration form; subsequent enrollments require an active session
// (handled server-side by the gate logic).

export function LoginPage(): React.JSX.Element {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<'login' | 'register'>('login')

  const onSuccess = (): void => {
    void queryClient.invalidateQueries({ queryKey: ['session'] })
    void navigate({ to: '/' })
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 p-4">
      {mode === 'login' ? (
        <LoginForm onSuccess={onSuccess} />
      ) : (
        <RegisterForm onSuccess={onSuccess} />
      )}
      <Button
        variant="link"
        onClick={() => {
          setMode((m) => (m === 'login' ? 'register' : 'login'))
        }}
      >
        {mode === 'login' ? 'Need to enroll a passkey?' : 'Already enrolled? Sign in'}
      </Button>
    </div>
  )
}
