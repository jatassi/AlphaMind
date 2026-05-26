// Theses dashboard page — /portfolio/theses (ALP-678).
//
// Filterable list of theses using TanStack Table.
// Filters are kept in URL search params (via TanStack Router) so deep-links
// preserve filter state.

import { useState } from 'react'

import type { ThesesFilters } from '@/api/portfolio'
import { useTheses } from '@/api/portfolio'

import { ThesesFilterBar } from './theses-filter-bar'
import { ThesesTable } from './theses-table'

export function ThesesPage(): React.JSX.Element {
  const [filters, setFilters] = useState<ThesesFilters>({ page: 1, page_size: 50 })
  const { data, isPending, isError } = useTheses(filters)

  if (isPending) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-muted-foreground">Loading theses…</span>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-destructive">Failed to load theses.</span>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Theses</h1>
      <ThesesFilterBar filters={filters} onFiltersChange={setFilters} />
      <ThesesTable theses={data.theses} total={data.total} />
    </div>
  )
}
