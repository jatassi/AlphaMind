import { useState } from 'react'

import { useMutation } from '@tanstack/react-query'

import { api, ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

import { createRegistrationCredential, type RegisterBeginEnvelope } from './webauthn'

// Registration form — first-launch setup-token flow + WebAuthn ceremony.
//
// Two-stage UX: (1) operator pastes the setup token printed by the daemon's
// first-boot log + enters a user name. (2) On submit, we POST /register/begin
// with both, drive navigator.credentials.create() against the returned
// envelope, then POST /register/complete with the credential. The complete
// response carries the issued session.

type TextFieldProps = {
  id: string
  label: string
  type: string
  value: string
  onValue: (v: string) => void
  required?: boolean
  autoComplete?: string
}

function TextField(props: TextFieldProps): React.JSX.Element {
  const { id, label, type, value, onValue, required, autoComplete } = props
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        value={value}
        onChange={(e) => {
          onValue(e.target.value)
        }}
        required={required}
        autoComplete={autoComplete}
      />
    </div>
  )
}

async function performRegistration(setupToken: string, userName: string): Promise<unknown> {
  const envelope = await api.post<RegisterBeginEnvelope>('/auth/register/begin', {
    setup_token: setupToken.length > 0 ? setupToken : null,
    user_name: userName,
  })
  const payload = await createRegistrationCredential(envelope)
  return api.post('/auth/register/complete', payload)
}

function extractErrorMessage(err: Error): string {
  const detail = err instanceof ApiError ? err.detail : null
  return typeof detail === 'string' ? detail : err.message
}

type FormBodyProps = {
  setupToken: string
  setSetupToken: (v: string) => void
  userName: string
  setUserName: (v: string) => void
  error: string | null
  isPending: boolean
  onSubmit: () => void
}

function FormBody(props: FormBodyProps): React.JSX.Element {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        props.onSubmit()
      }}
      className="space-y-4"
    >
      <TextField
        id="register-user-name"
        label="Operator name"
        type="text"
        value={props.userName}
        onValue={props.setUserName}
        required
        autoComplete="username"
      />
      <TextField
        id="register-setup-token"
        label="Setup token (first launch only)"
        type="password"
        value={props.setupToken}
        onValue={props.setSetupToken}
        autoComplete="off"
      />
      {props.error !== null && (
        <p role="alert" className="text-destructive text-sm">
          {props.error}
        </p>
      )}
      <Button type="submit" disabled={props.isPending} className="w-full">
        {props.isPending ? 'Registering...' : 'Register passkey'}
      </Button>
    </form>
  )
}

export function RegisterForm({ onSuccess }: { onSuccess?: () => void }): React.JSX.Element {
  const [setupToken, setSetupToken] = useState('')
  const [userName, setUserName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const mutation = useMutation({
    mutationFn: () => performRegistration(setupToken, userName),
    onSuccess: () => onSuccess?.(),
    onError: (err) => {
      setError(extractErrorMessage(err))
    },
  })
  return (
    <Card className="w-full max-w-md">
      <CardHeader>
        <CardTitle>Register a passkey</CardTitle>
      </CardHeader>
      <CardContent>
        <FormBody
          setupToken={setupToken}
          setSetupToken={setSetupToken}
          userName={userName}
          setUserName={setUserName}
          error={error}
          isPending={mutation.isPending}
          onSubmit={() => {
            setError(null)
            mutation.mutate()
          }}
        />
      </CardContent>
    </Card>
  )
}
