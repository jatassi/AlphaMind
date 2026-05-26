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
        <nav className="flex items-center gap-4 text-sm">
          {/* Story 05b (ALP-672) — live run watcher. */}
          <Link to="/live" className="hover:text-foreground/80 transition-colors">
            Live
          </Link>
          {/* Story 05c (ALP-673) — run history. */}
          <Link
            to="/history"
            search={{
              date_from: undefined,
              date_to: undefined,
              run_type: undefined,
              status: undefined,
              page: undefined,
              page_size: undefined,
            }}
            className="hover:underline"
          >
            Run history
          </Link>
          <Link
            to="/history/failures"
            search={{
              date_from: undefined,
              date_to: undefined,
              run_type: undefined,
              page: undefined,
              page_size: undefined,
            }}
            className="hover:underline"
          >
            Failure log
          </Link>
        </nav>
      </div>
    </header>
  )
}
