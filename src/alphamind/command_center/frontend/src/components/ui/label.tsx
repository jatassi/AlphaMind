import type * as React from 'react'

import { cn } from '@/lib/utils'

// shadcn/ui Label — form-label primitive. WebAuthn UI uses it on the
// setup-token + user-name fields; view stories use it on filters.
//
// The eslint-disable mirrors SlipStream's identical primitive — the rule
// can't be satisfied at the primitive level because the consumer's input
// `id` is what pairs the label, not the primitive itself. Callers must
// pass `htmlFor` matching their input's `id`.

function Label({ className, ...props }: React.ComponentProps<'label'>) {
  return (
    // eslint-disable-next-line jsx-a11y/label-has-associated-control
    <label
      className={cn(
        'text-sm leading-none font-medium peer-disabled:cursor-not-allowed peer-disabled:opacity-70',
        className,
      )}
      {...props}
    />
  )
}

export { Label }
