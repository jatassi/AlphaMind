import * as React from 'react'

import { type VariantProps } from 'class-variance-authority'

import { cn } from '@/lib/utils'

import { buttonVariants } from './button-variants'

// shadcn/ui-style Button — base primitive used by every actionable surface
// in the command center. Variants mirror shadcn defaults; the command-center
// design intentionally stays restrained (no extra variants) so per-view
// stories don't have to negotiate visual divergence.
//
// Intentionally omits shadcn's `asChild` / Radix Slot indirection — foundation
// scope only needs a plain `<button>` primitive. View stories that need slot
// behavior can add Radix as a dep then.

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> &
  VariantProps<typeof buttonVariants>

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, type, ...props }, ref) => (
    <button
      // type defaults to "button" — shadcn's default is "submit" which is
      // a footgun outside <form> contexts. Callers can pass type="submit"
      // explicitly when they mean it.
      type={type ?? 'button'}
      className={cn(buttonVariants({ variant, size, className }))}
      ref={ref}
      {...props}
    />
  ),
)
Button.displayName = 'Button'

export { Button }
