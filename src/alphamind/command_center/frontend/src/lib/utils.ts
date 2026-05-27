import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

// shadcn convention — `cn` merges Tailwind classes safely, dropping
// duplicates so a child can override a parent's utility class without
// fighting CSS specificity. Used by every `@/components/ui/*` primitive.
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
