import { useState } from 'react'

import { Link } from '@tanstack/react-router'

// Top-level navigation shell. View stories (05b-05j, 06a-06c) add nav items
// here as they introduce their routes; the foundation ships a minimal shell
// so the protected layout has something visible without per-view glue.
//
// Intentionally restrained — the command center is an operator console, not
// a marketing surface. Visual depth comes from the per-view layouts.

const HISTORY_SEARCH = {
  date_from: undefined,
  date_to: undefined,
  run_type: undefined,
  status: undefined,
  page: undefined,
  page_size: undefined,
}

const FAILURES_SEARCH = {
  date_from: undefined,
  date_to: undefined,
  run_type: undefined,
  page: undefined,
  page_size: undefined,
}

const CONFIG_LINKS: readonly { to: string; label: string }[] = [
  // Story 06b (ALP-683) — per-file YAML editor pages.
  { to: '/config/alerts', label: 'Alerts' },
  { to: '/config/security', label: 'Security' },
  { to: '/config/command-center', label: 'Command center' },
  { to: '/config/digest', label: 'Digest thresholds' },
]

function ConfigNavMenu(): React.JSX.Element {
  const [open, setOpen] = useState(false)
  return (
    <div className="relative" onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        className="text-muted-foreground hover:text-foreground transition-colors"
        onClick={() => setOpen((prev) => !prev)}
        onFocus={() => setOpen(true)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        Configuration
      </button>
      {open ? (
        <div
          className="bg-popover absolute right-0 z-10 mt-2 flex w-48 flex-col gap-1 rounded-md border p-2 text-sm shadow-md"
          role="menu"
        >
          {CONFIG_LINKS.map((entry) => (
            <Link
              key={entry.to}
              to={entry.to}
              className="hover:bg-muted rounded px-2 py-1"
              role="menuitem"
            >
              {entry.label}
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function NavLinks(): React.JSX.Element {
  return (
    <nav className="flex items-center gap-4 text-sm">
      {/* Story 05b (ALP-672) — live run watcher. */}
      <Link to="/live" className="hover:text-foreground/80 transition-colors">
        Live
      </Link>
      {/* Story 05c (ALP-673) — run history. */}
      <Link to="/history" search={HISTORY_SEARCH} className="hover:underline">
        Run history
      </Link>
      <Link to="/history/failures" search={FAILURES_SEARCH} className="hover:underline">
        Failure log
      </Link>
      {/* Story 05e (ALP-675) — activity log. */}
      <Link to="/activity-log" className="hover:text-foreground text-muted-foreground">
        Activity log
      </Link>
      {/* Story 05f (ALP-676) — portfolio dashboard. */}
      <Link
        to="/portfolio"
        className="text-muted-foreground hover:text-foreground transition-colors"
      >
        Portfolio
      </Link>
      {/* Story 06b (ALP-683) — config editor dropdown. */}
      <ConfigNavMenu />
      {/* Story 06c (ALP-684) — config diagnostic views. */}
      <Link
        to="/config/resolved"
        className="text-muted-foreground hover:text-foreground transition-colors"
      >
        Config
      </Link>
      <Link
        to="/config/history"
        className="text-muted-foreground hover:text-foreground transition-colors"
      >
        Config history
      </Link>
    </nav>
  )
}

export function Nav(): React.JSX.Element {
  return (
    <header className="border-b">
      <div className="container mx-auto flex h-14 items-center justify-between px-4">
        <Link to="/" className="font-semibold">
          AlphaMind Command Center
        </Link>
        <NavLinks />
      </div>
    </header>
  )
}
