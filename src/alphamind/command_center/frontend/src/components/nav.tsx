import { Link } from '@tanstack/react-router'

// Top-level navigation shell. View stories (05b–05j, 06a–06c) add nav items
// here as they introduce their routes; the foundation ships a minimal shell
// so the protected layout has something visible without per-view glue.
//
// Intentionally restrained — the command center is an operator console, not
// a marketing surface. Visual depth comes from the per-view layouts.

export function Nav(): React.JSX.Element {
  return (
    <header className="border-b">
      <div className="container mx-auto flex h-14 items-center justify-between px-4">
        <Link to="/" className="font-semibold">
          AlphaMind Command Center
        </Link>
        <nav className="flex items-center gap-4 text-sm" />
      </div>
    </header>
  )
}
