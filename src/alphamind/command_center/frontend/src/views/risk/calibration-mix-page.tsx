// Calibration mix page — /risk/calibration-mix (ALP-680).
//
// Recharts stacked-segment bar per invocation + 7-day stacked-area trend
// + stuck-in-non-calibrated panel-level warning list.
// Warm-up duration estimate from threshold-calibration.md cross-referenced.

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { useCalibrationMix } from '@/api/risk'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

const CAL_COLOR = '#16a34a'
const ACC_COLOR = '#ea580c'
const UNAV_COLOR = '#dc2626'

type BarPoint = {
  label: string
  calibrated: number
  accumulating: number
  unavailable: number
  total: number
  pct: number
}

type TooltipPayloadItem = {
  name?: string
  fill?: string
  value?: number
  payload?: BarPoint
}

function BarTooltipContent({
  payload,
  label,
}: {
  payload?: TooltipPayloadItem[]
  label?: string
}): React.JSX.Element | null {
  if (!payload?.length) {
    return null
  }
  const d = payload[0].payload
  if (d === undefined) {
    return null
  }
  return (
    <div className="bg-background rounded border p-2 text-xs shadow">
      <p className="font-medium">{label ?? ''}</p>
      <p>
        {d.pct.toFixed(1)}% calibrated / {d.total} total
      </p>
      {payload.map((p) => (
        <p key={p.name} style={{ color: p.fill }}>
          {p.name}: {p.value ?? 0}
        </p>
      ))}
    </div>
  )
}

type StuckBlock = {
  block_id: string
  reason: string
  last_calibrated_invocation_id: string | null
}

function StuckBlocksList({
  blocks,
  warmup,
}: {
  blocks: StuckBlock[]
  warmup: string
}): React.JSX.Element {
  return (
    <Card className="border-orange-400">
      <CardHeader>
        <CardTitle className="text-orange-700">Stuck blocks ({blocks.length})</CardTitle>
      </CardHeader>
      <CardContent className="text-sm">
        <p className="text-muted-foreground mb-3">
          The following output blocks are stuck in accumulating or unavailable state.
        </p>
        <p className="bg-muted mb-4 rounded-md p-3 text-xs">
          <strong>Warm-up duration estimate:</strong> {warmup}
        </p>
        <ul className="space-y-1">
          {blocks.map((b) => (
            <li key={b.block_id} className="bg-muted/50 rounded-sm px-2 py-1">
              <span className="font-mono text-xs font-medium">{b.block_id}</span>
              <span className="text-muted-foreground ml-2">— {b.reason}</span>
              {b.last_calibrated_invocation_id === null ? null : (
                <span className="text-muted-foreground ml-2 text-xs">
                  (last calibrated: {b.last_calibrated_invocation_id.slice(0, 8)})
                </span>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}

function PerInvocationChart({ barData }: { barData: BarPoint[] }): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Per-invocation calibration mix</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={barData}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="label" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 11 }} />
            <Tooltip content={BarTooltipContent} />
            <Legend />
            <Bar dataKey="calibrated" stackId="a" fill={CAL_COLOR} name="calibrated">
              {barData.map((_, i) => (
                <Cell key={`cal-${String(i)}`} fill={CAL_COLOR} />
              ))}
            </Bar>
            <Bar dataKey="accumulating" stackId="a" fill={ACC_COLOR} name="accumulating" />
            <Bar dataKey="unavailable" stackId="a" fill={UNAV_COLOR} name="unavailable" />
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}

type TrendPoint = { date: string; calibrated_pct: number; total_blocks: number }

function TrendChart({ trendData }: { trendData: TrendPoint[] }): React.JSX.Element {
  return (
    <Card>
      <CardHeader>
        <CardTitle>7-day calibration trend</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={180}>
          <AreaChart data={trendData}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} />
            <YAxis
              domain={[0, 100]}
              tick={{ fontSize: 11 }}
              tickFormatter={(v: number) => `${v}%`}
            />
            <Tooltip formatter={(value: unknown) => `${(value as number).toFixed(1)}%`} />
            <Area
              type="monotone"
              dataKey="calibrated_pct"
              stroke={CAL_COLOR}
              fill={`${CAL_COLOR}33`}
              name="Calibrated %"
            />
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  )
}

type CalibrationData = ReturnType<typeof useCalibrationMix>['data'] & {}

function toBarData(data: CalibrationData): BarPoint[] {
  return data.per_invocation.map((inv) => ({
    label: inv.invocation_id.slice(0, 8),
    calibrated: inv.calibrated,
    accumulating: inv.accumulating,
    unavailable: inv.unavailable,
    total: inv.total_blocks,
    pct: inv.calibrated_pct,
  }))
}

function toTrendData(data: CalibrationData): TrendPoint[] {
  return data.seven_day_trend.map((pt) => ({
    date: pt.date.slice(5),
    calibrated_pct: pt.calibrated_pct,
    total_blocks: pt.total_blocks,
  }))
}

function EmptyState({ warmup }: { warmup: string }): React.JSX.Element {
  return (
    <Card>
      <CardContent className="p-8">
        <p className="text-muted-foreground text-center text-sm">
          No calibration data found. Ensure provenance files exist at{' '}
          <code>data/provenance/invocations/&lt;id&gt;/data_calibration_state.json</code>.
        </p>
        <p className="bg-muted text-muted-foreground mt-4 rounded-md p-3 text-xs">
          <strong>Warm-up duration estimate:</strong> {warmup}
        </p>
      </CardContent>
    </Card>
  )
}

export function CalibrationMixPage(): React.JSX.Element {
  const { data, isPending, isError } = useCalibrationMix()

  if (isPending) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-muted-foreground">Loading calibration mix…</span>
      </div>
    )
  }

  if (isError) {
    return (
      <div className="flex items-center justify-center p-8">
        <span className="text-destructive">Failed to load calibration data.</span>
      </div>
    )
  }

  const barData = toBarData(data)
  const trendData = toTrendData(data)

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">Calibration mix</h1>
      {data.stuck_blocks.length > 0 ? (
        <StuckBlocksList blocks={data.stuck_blocks} warmup={data.warmup_duration_estimate} />
      ) : null}
      {barData.length > 0 ? <PerInvocationChart barData={barData} /> : null}
      {trendData.length > 0 ? <TrendChart trendData={trendData} /> : null}
      {barData.length === 0 && trendData.length === 0 ? (
        <EmptyState warmup={data.warmup_duration_estimate} />
      ) : null}
    </div>
  )
}
