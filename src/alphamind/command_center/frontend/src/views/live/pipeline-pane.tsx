import { useState } from 'react'

import { useMutation, useQueryClient } from '@tanstack/react-query'

import { api } from '@/api/client'
import { type EventStreamMessage, useEventStream } from '@/api/events'
import { type InvocationStatus, liveViewQueryOptions, useLiveView } from '@/api/queries'

// Pipeline status pane (story 05b / ALP-672).
//
// Renders the current / most-recent invocation summary + Pause / Resume /
// Trigger emergency invocation action buttons, each gated by a confirmation
// modal.

type ActionKind = 'pause' | 'resume' | 'emergency'

type ActionState = {
  open: boolean
  kind: ActionKind | null
  error: string | null
}

const CLOSED: ActionState = { open: false, kind: null, error: null }

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function nullInv(): InvocationStatus {
  return {
    invocation_id: null,
    run_type: null,
    started_at: null,
    ended_at: null,
    status: null,
    current_phase: null,
    phase_durations: null,
    agent_metrics: null,
    retry_count: null,
    error_summary: null,
  }
}

function postControl(kind: ActionKind, token: string): Promise<unknown> {
  switch (kind) {
    case 'pause': {
      return api.post('/api/control/pause', { reason: token || 'operator pause' })
    }
    case 'resume': {
      return api.post('/api/control/resume', {})
    }
    case 'emergency': {
      return api.post('/api/control/trigger_emergency_invocation', { reason: token })
    }
  }
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function InvocationCard({ inv }: { inv: InvocationStatus }): React.JSX.Element {
  if (inv.invocation_id === null) {
    return <p className="text-muted-foreground text-sm">No invocation data.</p>
  }
  return (
    <dl className="space-y-1 text-sm">
      <div className="flex gap-2">
        <dt className="font-medium">ID</dt>
        <dd className="font-mono text-xs">{inv.invocation_id}</dd>
      </div>
      <div className="flex gap-2">
        <dt className="font-medium">Type</dt>
        <dd>{inv.run_type ?? '—'}</dd>
      </div>
      <div className="flex gap-2">
        <dt className="font-medium">Status</dt>
        <dd>{inv.status ?? 'unknown'}</dd>
      </div>
    </dl>
  )
}

type TokenInputProps = { value: string; onChange: (v: string) => void }

function TokenInput({ value, onChange }: TokenInputProps): React.JSX.Element {
  return (
    <div className="mb-4 space-y-2">
      <p className="text-muted-foreground text-sm">Type a reason for the emergency invocation.</p>
      <input
        type="text"
        className="w-full rounded border px-3 py-2 text-sm"
        placeholder="Reason (required)"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
    </div>
  )
}

type ModalBodyProps = {
  kind: ActionKind
  token: string
  onTokenChange: (v: string) => void
  error: string | null
}

function ModalBody({ kind, token, onTokenChange, error }: ModalBodyProps): React.JSX.Element {
  return (
    <>
      {kind === 'emergency' ? (
        <TokenInput value={token} onChange={onTokenChange} />
      ) : (
        <p className="text-muted-foreground mb-4 text-sm">
          {kind === 'pause'
            ? 'Pause the scheduler? Subsequent triggers will be skipped until resumed.'
            : 'Resume the scheduler? Triggers will fire normally from the next window.'}
        </p>
      )}
      {error !== null && <p className="text-destructive mb-3 text-sm">{error}</p>}
    </>
  )
}

type ConfirmModalProps = {
  action: ActionState
  token: string
  onTokenChange: (v: string) => void
  onConfirm: () => void
  onCancel: () => void
  isPending: boolean
}

function ConfirmModal(props: ConfirmModalProps): React.JSX.Element | null {
  const { action, token, onTokenChange, onConfirm, onCancel, isPending } = props
  if (!action.open || action.kind === null) {
    return null
  }
  const labels: Record<ActionKind, string> = {
    pause: 'Pause',
    resume: 'Resume',
    emergency: 'Trigger Emergency Invocation',
  }
  const label = labels[action.kind]
  const confirmDisabled = isPending || (action.kind === 'emergency' && token.trim() === '')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-background w-full max-w-sm rounded-lg p-6 shadow-lg">
        <h2 className="mb-3 text-lg font-semibold">{label}</h2>
        <ModalBody
          kind={action.kind}
          token={token}
          onTokenChange={onTokenChange}
          error={action.error}
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="hover:bg-muted rounded px-3 py-1.5 text-sm"
            disabled={isPending}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="bg-primary text-primary-foreground hover:bg-primary/90 rounded px-3 py-1.5 text-sm disabled:opacity-50"
            disabled={confirmDisabled}
          >
            {isPending ? 'Sending…' : 'Confirm'}
          </button>
        </div>
      </div>
    </div>
  )
}

type ActionButtonsProps = { onOpen: (kind: ActionKind) => void }

function ActionButtons({ onOpen }: ActionButtonsProps): React.JSX.Element {
  return (
    <div className="mt-4 flex flex-wrap gap-2">
      <button
        type="button"
        onClick={() => onOpen('pause')}
        className="hover:bg-muted rounded border px-3 py-1.5 text-sm"
      >
        Pause
      </button>
      <button
        type="button"
        onClick={() => onOpen('resume')}
        className="hover:bg-muted rounded border px-3 py-1.5 text-sm"
      >
        Resume
      </button>
      <button
        type="button"
        onClick={() => onOpen('emergency')}
        className="border-destructive text-destructive hover:bg-destructive/10 rounded border px-3 py-1.5 text-sm"
      >
        Trigger Emergency
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main pane
// ---------------------------------------------------------------------------

function mergePipelineEvent(
  prev: InvocationStatus | null,
  payload: Record<string, unknown>,
): InvocationStatus {
  const base = prev ?? nullInv()
  return {
    ...base,
    invocation_id: (payload.invocation_id as string | null) ?? base.invocation_id,
    status: (payload.status as string | null) ?? base.status,
    current_phase: (payload.phase as string | null) ?? base.current_phase,
  }
}

function usePipelineEventState(): [InvocationStatus | null, (m: EventStreamMessage) => void] {
  const [state, setState] = useState<InvocationStatus | null>(null)
  const update = (msg: EventStreamMessage): void => {
    const payload = msg.payload as Record<string, unknown>
    setState((prev) => mergePipelineEvent(prev, payload))
  }
  return [state, update]
}

type PipelinePaneState = {
  action: ActionState
  token: string
  setAction: (a: ActionState) => void
  setToken: (t: string) => void
}

function usePipelinePane(): PipelinePaneState & {
  mutate: (t: string) => void
  isPending: boolean
} {
  const queryClient = useQueryClient()
  const [action, setAction] = useState<ActionState>(CLOSED)
  const [token, setToken] = useState('')
  const mutation = useMutation({
    mutationFn: (t: string) => postControl(action.kind ?? 'pause', t),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: liveViewQueryOptions().queryKey })
      setAction(CLOSED)
      setToken('')
    },
    onError: (err: unknown) => {
      setAction((prev) => ({
        ...prev,
        error: err instanceof Error ? err.message : 'Request failed',
      }))
    },
  })
  return {
    action,
    token,
    setAction,
    setToken,
    mutate: (t) => mutation.mutate(t),
    isPending: mutation.isPending,
  }
}

export function PipelinePane(): React.JSX.Element {
  const liveQuery = useLiveView()
  const [pipelineState, updateFromEvent] = usePipelineEventState()
  const { action, token, setAction, setToken, mutate, isPending } = usePipelinePane()

  useEventStream({
    subscribers: {
      'pipeline:invocation_started': updateFromEvent,
      'pipeline:invocation_ended': updateFromEvent,
      'pipeline:phase_transition': updateFromEvent,
    },
  })

  const inv = pipelineState ?? liveQuery.data?.pipeline ?? nullInv()
  const openAction = (kind: ActionKind): void => {
    setAction({ open: true, kind, error: null })
    setToken('')
  }
  const closeAction = (): void => {
    setAction(CLOSED)
    setToken('')
  }

  return (
    <div className="rounded-lg border p-4">
      <h2 className="mb-3 text-base font-semibold">Pipeline Status</h2>
      {liveQuery.isError ? (
        <p className="text-destructive mb-2 text-sm">Failed to load pipeline status.</p>
      ) : null}
      <InvocationCard inv={inv} />
      <ActionButtons onOpen={openAction} />
      <ConfirmModal
        action={action}
        token={token}
        onTokenChange={setToken}
        onConfirm={() => mutate(token)}
        onCancel={closeAction}
        isPending={isPending}
      />
    </div>
  )
}
