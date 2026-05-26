// Portfolio dashboard page — /portfolio (ALP-676).
//
// Composes the five panes vertically.  Live updates via useEventStream:
// - monitor:fill_received  → refetch (position row mutations)
// - pipeline:invocation_ended → refetch (portfolio_summary refresh)
//
// Position rows deep-link to /portfolio/positions/$positionId (story 05g).

import { useCallback, useMemo } from 'react'

import { useQueryClient } from '@tanstack/react-query'

import { useEventStream } from '@/api/events'
import { portfolioDashboardQueryOptions, usePortfolioDashboard } from '@/api/portfolio'

import { CashCapitalPane } from './cash-capital-pane'
import { EquityPlPane } from './equity-pl-pane'
import { ExposurePane } from './exposure-pane'
import { PendingOrdersTable } from './pending-orders-table'
import { PositionsTable } from './positions-table'

export function PortfolioDashboardPage(): React.JSX.Element {
  const queryClient = useQueryClient()
  const { data, isPending, isError } = usePortfolioDashboard()

  // Stable refetch callback — invalidates the portfolio dashboard cache.
  const refetch = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: portfolioDashboardQueryOptions().queryKey })
  }, [queryClient])

  // Stable subscribers map — useEventStream reads from this on every event
  // dispatch; identity is stable as long as refetch identity is stable.
  const subscribers = useMemo(
    () => ({
      'monitor:fill_received': refetch,
      'pipeline:invocation_ended': refetch,
    }),
    [refetch],
  )

  useEventStream({ subscribers })

  if (isPending) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-muted-foreground">Loading portfolio…</span>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-destructive">Failed to load portfolio data.</span>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Portfolio</h1>
      <div className="grid gap-4 md:grid-cols-2">
        <EquityPlPane data={data.equity_and_pl} />
        <CashCapitalPane data={data.cash_and_capital} />
      </div>
      <ExposurePane data={data.exposure} />
      <PositionsTable positions={data.positions} />
      <PendingOrdersTable orders={data.pending_orders} />
    </div>
  )
}
