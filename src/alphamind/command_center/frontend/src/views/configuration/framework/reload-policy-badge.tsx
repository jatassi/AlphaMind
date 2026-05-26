// ReloadPolicyBadge — per-field badge that surfaces the field's reload
// policy (ALP-679). The schema endpoint emits `invocation_time` or
// `deploy_time` per field; the badge renders the human-readable label
// + a color cue.

import { cn } from '@/lib/utils'

export type ReloadPolicyValue = 'invocation_time' | 'deploy_time'

type ReloadPolicyBadgeProps = {
  policy: ReloadPolicyValue
  className?: string
}

const LABELS: Record<ReloadPolicyValue, string> = {
  invocation_time: 'Invocation-time',
  deploy_time: 'Deploy-time',
}

const COLORS: Record<ReloadPolicyValue, string> = {
  invocation_time: 'border-border bg-muted text-muted-foreground',
  // Deploy-time gets a warm-tone reminder so the operator notices the
  // restart requirement at a glance.
  deploy_time: 'border-amber-500 bg-amber-100 text-amber-900',
}

export function ReloadPolicyBadge({
  policy,
  className,
}: ReloadPolicyBadgeProps): React.JSX.Element {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        COLORS[policy],
        className,
      )}
    >
      {LABELS[policy]}
    </span>
  )
}
