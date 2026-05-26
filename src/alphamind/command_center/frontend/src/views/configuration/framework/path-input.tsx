// PathInput — text input with a file-existence indicator (ALP-679).
// Probes the backend's GET /api/views/config/path-exists?path=... endpoint
// on path change; surfaces "exists" / "missing" inline so the operator
// catches typos before saving.

import { useEffect, useState } from 'react'

import { cn } from '@/lib/utils'

type PathInputProps = {
  path: string
  label: string
  value: string
  onChange: (next: string) => void
  disabled?: boolean
  className?: string
}

type ProbeState = 'unknown' | 'exists' | 'missing'

async function probePath(candidate: string): Promise<ProbeState> {
  if (candidate.trim() === '') {
    return 'unknown'
  }
  try {
    const response = await fetch(
      `/api/views/config/path-exists?path=${encodeURIComponent(candidate)}`,
    )
    if (!response.ok) {
      return 'unknown'
    }
    const body = (await response.json()) as { exists?: boolean }
    return body.exists === true ? 'exists' : 'missing'
  } catch {
    return 'unknown'
  }
}

function ExistenceIndicator({ state }: { state: ProbeState }): React.JSX.Element | null {
  if (state === 'exists') {
    return <span className="text-xs text-green-600">exists</span>
  }
  if (state === 'missing') {
    return <span className="text-destructive text-xs">missing</span>
  }
  return null
}

export function PathInput({
  path,
  label,
  value,
  onChange,
  disabled,
  className,
}: PathInputProps): React.JSX.Element {
  const [probeState, setProbeState] = useState<ProbeState>('unknown')

  useEffect(() => {
    let cancelled = false
    void probePath(value).then((next) => {
      if (!cancelled) {
        setProbeState(next)
      }
    })
    return () => {
      cancelled = true
    }
  }, [value])

  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label htmlFor={path} className="text-sm font-medium">
        {label}
      </label>
      <div className="flex items-center gap-2">
        <input
          id={path}
          type="text"
          value={value}
          disabled={disabled}
          onChange={(e) => {
            onChange(e.target.value)
          }}
          className="border-input bg-background ring-offset-background focus-visible:ring-ring h-10 w-full rounded-md border px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-50"
        />
        <ExistenceIndicator state={probeState} />
      </div>
    </div>
  )
}
