// Position detail page — /portfolio/positions/$positionId (ALP-677).
//
// Three tabs: State (position fields + bracket legs + fills),
//             Thesis (thesis summary + components),
//             History (activity log pre-filtered by position_id).
//
// Page header includes a Force Close button gated by typed-token
// confirmation modal.

import { useState } from 'react'

import { Link, useParams } from '@tanstack/react-router'

import { type PositionDetail, usePositionDetail } from '@/api/portfolio'
import { Button } from '@/components/ui/button'

import { ForceCloseModal } from './force-close-modal'
import { HistoryTab } from './history-tab'
import { StateTab } from './state-tab'
import { ThesisTab } from './thesis-tab'

type Tab = 'state' | 'thesis' | 'history'

const TAB_DEFS: { id: Tab; label: string }[] = [
  { id: 'state', label: 'State' },
  { id: 'thesis', label: 'Thesis' },
  { id: 'history', label: 'History' },
]

function tabClass(isActive: boolean): string {
  const base = 'mr-1 rounded-t px-4 py-2 text-sm font-medium transition-colors '
  return isActive
    ? `${base}border-primary text-primary border-b-2`
    : `${base}text-muted-foreground hover:text-foreground`
}

function TabBar({
  active,
  onChange,
}: {
  active: Tab
  onChange: (tab: Tab) => void
}): React.JSX.Element {
  return (
    <div className="border-b" role="tablist">
      {TAB_DEFS.map(({ id, label }) => (
        <button
          key={id}
          role="tab"
          aria-selected={active === id}
          className={tabClass(active === id)}
          onClick={() => onChange(id)}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

function PositionTabs({
  data,
  positionId,
  activeTab,
  onChange,
}: {
  data: PositionDetail
  positionId: string
  activeTab: Tab
  onChange: (tab: Tab) => void
}): React.JSX.Element {
  return (
    <>
      <TabBar active={activeTab} onChange={onChange} />
      <div role="tabpanel">
        {activeTab === 'state' && <StateTab data={data} />}
        {activeTab === 'thesis' && <ThesisTab thesis={data.thesis} />}
        {activeTab === 'history' && (
          <HistoryTab positionId={positionId} entries={data.activity_log} />
        )}
      </div>
    </>
  )
}

export function PositionDetailPage(): React.JSX.Element {
  const rawParams = useParams({ strict: false })
  const positionId = typeof rawParams.positionId === 'string' ? rawParams.positionId : ''
  const { data, isPending, isError } = usePositionDetail(positionId)
  const [activeTab, setActiveTab] = useState<Tab>('state')
  const [showModal, setShowModal] = useState(false)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)

  if (isPending) {
    return <LoadingState msg="Loading position…" />
  }
  if (isError) {
    return <ErrorState msg="Failed to load position detail." />
  }

  function handleForceCloseSuccess(envelopeId: string): void {
    setShowModal(false)
    setSuccessMsg(`Force close accepted — envelope ${envelopeId}`)
  }

  return (
    <div className="space-y-4">
      <PageHeader
        ticker={data.ticker}
        instrumentType={data.instrument_type}
        direction={data.direction}
        status={data.status}
        onForceClose={() => setShowModal(true)}
      />
      {successMsg === null ? null : <SuccessBanner msg={successMsg} />}
      <PositionTabs
        data={data}
        positionId={positionId}
        activeTab={activeTab}
        onChange={setActiveTab}
      />
      {showModal ? (
        <ForceCloseModal
          positionId={positionId}
          ticker={data.ticker}
          onClose={() => setShowModal(false)}
          onSuccess={handleForceCloseSuccess}
        />
      ) : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Extracted sub-components to stay within the 50-line per-function limit
// ---------------------------------------------------------------------------

type PageHeaderProps = {
  ticker: string
  instrumentType: string
  direction: string | null
  status: string
  onForceClose: () => void
}

function PageHeader({
  ticker,
  instrumentType,
  direction,
  status,
  onForceClose,
}: PageHeaderProps): React.JSX.Element {
  return (
    <>
      <nav className="text-muted-foreground text-sm" aria-label="Breadcrumb">
        <Link to="/portfolio" className="hover:text-foreground">
          Portfolio
        </Link>
        {' / '}
        <span>{ticker}</span>
      </nav>
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{ticker}</h1>
          <p className="text-muted-foreground text-sm">
            {instrumentType} · {direction ?? 'N/A'} · {status}
          </p>
        </div>
        <Button variant="destructive" size="sm" onClick={onForceClose} disabled={status !== 'OPEN'}>
          Force close
        </Button>
      </div>
    </>
  )
}

function SuccessBanner({ msg }: { msg: string }): React.JSX.Element {
  return (
    <div className="rounded border border-green-300 bg-green-50 px-4 py-3 text-sm text-green-800">
      {msg}
    </div>
  )
}

function LoadingState({ msg }: { msg: string }): React.JSX.Element {
  return (
    <div className="flex items-center justify-center p-8">
      <span className="text-muted-foreground">{msg}</span>
    </div>
  )
}

function ErrorState({ msg }: { msg: string }): React.JSX.Element {
  return (
    <div className="flex items-center justify-center p-8">
      <span className="text-destructive">{msg}</span>
    </div>
  )
}
