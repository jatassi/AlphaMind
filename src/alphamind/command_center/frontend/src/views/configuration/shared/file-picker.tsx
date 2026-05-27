// FilePicker — sidebar nav for config-file families (ALP-682).
//
// Renders a vertical list of links derived from the ``slugs`` array
// returned by GET /api/views/config/files?family=<family>. Each link
// deep-links to the per-file editor page. The active slug is highlighted.
//
// ``basePath`` is the route prefix (e.g. "/config/profiles"); the link
// href is ``basePath + "/" + shortName`` where ``shortName`` is the last
// segment of the slug after the ``<family>/`` prefix.

import { cn } from '@/lib/utils'

type FilePickerProps = {
  family: string
  slugs: readonly string[]
  activeSlug: string
  basePath: string
}

function slugToName(slug: string): string {
  // "profiles/small" → "small"
  const slash = slug.lastIndexOf('/')
  return slash === -1 ? slug : slug.slice(slash + 1)
}

export function FilePicker({
  family: _family,
  slugs,
  activeSlug,
  basePath,
}: FilePickerProps): React.JSX.Element {
  return (
    <nav aria-label="Config file picker" className="flex flex-col gap-1">
      {slugs.map((slug) => {
        const name = slugToName(slug)
        const href = `${basePath}/${name}`
        const isActive = slug === activeSlug
        return (
          <a
            key={slug}
            href={href}
            className={cn(
              'rounded-md px-3 py-1.5 text-sm',
              isActive
                ? 'bg-primary text-primary-foreground font-medium'
                : 'text-muted-foreground hover:bg-muted',
            )}
          >
            {name}
          </a>
        )
      })}
    </nav>
  )
}
