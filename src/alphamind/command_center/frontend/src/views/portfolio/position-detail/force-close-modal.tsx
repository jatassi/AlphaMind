// Force Close confirmation modal (ALP-677).
//
// Operator must type the position ticker before the submit button activates.
// On confirm, POSTs to /api/control/force_close_position with a rationale.
// Success/error surfaces as an inline message inside the modal.

import { useState } from 'react'

import { api, ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

type ForceCloseResponse = {
  status: string
  applied_at: string
  envelope_id: string
}

type Props = {
  positionId: string
  ticker: string
  onClose: () => void
  onSuccess: (envelopeId: string) => void
}

function extractErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return 'Unexpected error — please try again.'
  }
  const detail = error.detail
  if (
    typeof detail === 'object' &&
    detail !== null &&
    'error' in detail &&
    typeof (detail as Record<string, unknown>).error === 'object'
  ) {
    const errObj = (detail as Record<string, Record<string, unknown>>).error
    const msg = errObj.detail
    return typeof msg === 'string' ? msg : error.message
  }
  return error.message
}

async function submitForceClose(
  positionId: string,
  rationale: string,
): Promise<ForceCloseResponse> {
  return api.post<ForceCloseResponse>('/api/control/force_close_position', {
    position_id: positionId,
    rationale,
  })
}

type FormFieldsProps = {
  ticker: string
  typedToken: string
  rationale: string
  errorMsg: string | null
  isPending: boolean
  canSubmit: boolean
  onTokenChange: (v: string) => void
  onRationaleChange: (v: string) => void
  onClose: () => void
  onSubmit: () => void
}

function FormFields(p: FormFieldsProps): React.JSX.Element {
  const { ticker, typedToken, rationale, errorMsg, isPending, canSubmit } = p
  const { onTokenChange, onRationaleChange, onClose, onSubmit } = p
  return (
    <div className="space-y-4">
      <div>
        <Label htmlFor="confirm-ticker">
          Type <strong>{ticker}</strong> to confirm
        </Label>
        <Input
          id="confirm-ticker"
          value={typedToken}
          onChange={(e) => onTokenChange(e.target.value)}
          placeholder={ticker}
          className="mt-1"
          autoComplete="off"
        />
      </div>
      <div>
        <Label htmlFor="force-close-rationale">Rationale</Label>
        <Input
          id="force-close-rationale"
          value={rationale}
          onChange={(e) => onRationaleChange(e.target.value)}
          placeholder="Reason for force close…"
          className="mt-1"
        />
      </div>
      {errorMsg === null ? null : (
        <p className="text-destructive text-sm" role="alert">
          {errorMsg}
        </p>
      )}
      <div className="flex justify-end gap-3">
        <Button variant="outline" onClick={onClose} disabled={isPending}>
          Cancel
        </Button>
        <Button variant="destructive" disabled={!canSubmit} onClick={onSubmit}>
          {isPending ? 'Submitting…' : 'Force close'}
        </Button>
      </div>
    </div>
  )
}

function ForceCloseForm({ positionId, ticker, onClose, onSuccess }: Props): React.JSX.Element {
  const [typedToken, setTypedToken] = useState('')
  const [rationale, setRationale] = useState('')
  const [isPending, setIsPending] = useState(false)
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  const tokenMatches = typedToken.trim().toUpperCase() === ticker.toUpperCase()
  const canSubmit = tokenMatches && rationale.trim().length > 0 && !isPending

  function handleSubmit(): void {
    if (!canSubmit) {
      return
    }
    setIsPending(true)
    setErrorMsg(null)
    void submitForceClose(positionId, rationale.trim())
      .then((resp) => {
        onSuccess(resp.envelope_id)
      })
      .catch((error: unknown) => {
        setErrorMsg(extractErrorMessage(error))
        setIsPending(false)
      })
  }

  return (
    <FormFields
      ticker={ticker}
      typedToken={typedToken}
      rationale={rationale}
      errorMsg={errorMsg}
      isPending={isPending}
      canSubmit={canSubmit}
      onTokenChange={setTypedToken}
      onRationaleChange={setRationale}
      onClose={onClose}
      onSubmit={handleSubmit}
    />
  )
}

export function ForceCloseModal(props: Props): React.JSX.Element {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="force-close-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
    >
      <div className="bg-background w-full max-w-md rounded-lg p-6 shadow-xl">
        <h2 id="force-close-title" className="text-destructive mb-1 text-lg font-semibold">
          Force close position
        </h2>
        <p className="text-muted-foreground mb-4 text-sm">
          This will immediately submit a market close order for <strong>{props.ticker}</strong>.
          This action cannot be undone.
        </p>
        <ForceCloseForm {...props} />
      </div>
    </div>
  )
}
