// Pagination controls for the activity-log table.

import { Button } from '@/components/ui/button'

type Props = {
  page: number
  pageSize: number
  total: number
  hasMore: boolean
  onPageChange: (page: number) => void
}

export function PaginationControls({
  page,
  pageSize,
  total,
  hasMore,
  onPageChange,
}: Props): React.JSX.Element {
  const start = (page - 1) * pageSize + 1
  const end = Math.min(page * pageSize, total)

  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-muted-foreground">
        {total === 0 ? 'No results' : `${start}–${end} of ${total}`}
      </span>
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          Previous
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={!hasMore}
          onClick={() => onPageChange(page + 1)}
        >
          Next
        </Button>
      </div>
    </div>
  )
}
