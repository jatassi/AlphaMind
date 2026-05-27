// AlertsConfigPage — config/alerts.yaml editor (ALP-683).
//
// Wraps the shared ConfigEditorPage scaffold with two alerts-specific
// touches:
//
//  * "Alert engine reloaded" toast on save success — surfaces the
//    engine's hot-reload feedback so the operator confirms the edit
//    took effect without a restart (story 06b AC).
//  * Listens for the ``cc:config_reload_requires_restart`` SSE event
//    so a channels-section edit (which can't be hot-applied) flips
//    the page into a "restart required" state without a refresh.

import { useState } from 'react'

import { useEventStream } from '@/api/events'
import { ConfigEditorPage } from '@/views/configuration/config-editor-page'

type EngineToast =
  | { kind: 'hidden' }
  | { kind: 'reloaded' }
  | { kind: 'restart_required'; reason: string }

function EngineFeedbackBanner({ toast }: { toast: EngineToast }): React.JSX.Element | null {
  if (toast.kind === 'hidden') {
    return null
  }
  if (toast.kind === 'reloaded') {
    return (
      <div
        role="status"
        className="bg-muted text-foreground rounded-md px-3 py-2 text-sm"
        data-testid="alert-engine-reloaded-toast"
      >
        Alert engine reloaded.
      </div>
    )
  }
  return (
    <div
      role="alert"
      className="bg-destructive/10 text-destructive rounded-md px-3 py-2 text-sm"
      data-testid="alert-engine-restart-required-banner"
    >
      Restart required to apply changes ({toast.reason}).
    </div>
  )
}

export function AlertsConfigPage(): React.JSX.Element {
  const [toast, setToast] = useState<EngineToast>({ kind: 'hidden' })
  useEventStream({
    subscribers: {
      'cc:config_reload_requires_restart': (msg) => {
        const payload = msg.payload as { reason?: string }
        setToast({ kind: 'restart_required', reason: payload.reason ?? 'unknown' })
      },
    },
  })
  return (
    <div className="flex flex-col gap-4">
      <EngineFeedbackBanner toast={toast} />
      <ConfigEditorPage
        configFileSlug="alerts"
        title="Alerts configuration"
        onSaved={(result) => {
          if (!result.deployTimeFieldsChanged) {
            setToast({ kind: 'reloaded' })
          }
        }}
      />
    </div>
  )
}
