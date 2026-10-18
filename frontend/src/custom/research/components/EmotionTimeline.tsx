/**
 * EmotionTimeline — P2 v2.0f/g 升级: 5 曲线时间轴 + 拆分组件.
 *
 * 拆分后:
 *   - EmotionTimelineChart:    5 曲线 SVG (无 legend / 无 card)
 *   - EmotionTimelineLatestCard: 最近交易日概览卡
 *   - EmotionTimelineLegend:   曲线 legend (含 title + 元信息)
 *   - EmotionTimeline:         完整布局 (向后兼容 mock 模式)
 *
 * 拆分目的是: 让 IRP ShortTermWorkspace 把 chart / card / PromotionByBoard
 * 三者放进 grid, 共享等高 (高度由最高那个决定, 其余 stretch).
 *
 * 净室重写: SVG 自渲染 (无外部图表库依赖), 颜色/线型借鉴 limit-ladder.
 */
import { useState } from 'react'
import { Activity, Loader2 } from 'lucide-react'
import { DatePicker } from '@/components/DatePicker'
import type { HistorySeries } from '../mocks/api'

interface SeriesConfig {
  key: keyof HistorySeries['series']
  label: string
  color: string
  dashed?: boolean
}

const SERIES: SeriesConfig[] = [
  { key: 'limit_up', label: '涨停', color: '#f43f5e' },
  { key: 'broken', label: '破板', color: '#a855f7', dashed: true },
  { key: 'limit_down', label: '跌停', color: '#10b981' },
  { key: 'recovery', label: '翘板', color: '#eab308', dashed: true },
  { key: 'is_one_word', label: '一字板', color: '#3b82f6' },
]

/** Shared hook: 控制曲线显隐 (export, 让父组件持有状态共享给 Legend + Chart) */
export function useSeriesVisibility() {
  const [hidden, setHidden] = useState<Set<string>>(new Set())
  const toggle = (key: string) => {
    setHidden((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }
  return { hidden, toggle }
}

/** 找数据非 0 的最近一天 (用于 "最近交易日" 卡片) */
function findLatestNonEmpty(history: HistorySeries) {
  const last = history.per_day[history.per_day.length - 1] ?? null
  return (
    [...history.per_day].reverse().find((d) => d.limit_up > 0 || d.limit_down > 0) ?? last
  )
}

// === Chart (SVG only, 自适应高度填充父容器) ===
export function EmotionTimelineChart({
  history,
  hidden,
}: {
  history: HistorySeries
  hidden: Set<string>
}) {
  const dates = history.dates
  const N = dates.length
  const maxY = Math.max(
    1,
    ...SERIES.flatMap((s) => history.series[s.key] as number[]),
  )
  const yTop = Math.ceil(maxY / 5) * 5 || 5

  const w = 720
  const h = 200
  const paddingL = 36
  const paddingR = 12
  const paddingT = 16
  const paddingB = 24
  const innerW = w - paddingL - paddingR
  const innerH = h - paddingT - paddingB
  const step = innerW / Math.max(N - 1, 1)

  const yFor = (v: number) => paddingT + (1 - v / yTop) * innerH
  const xFor = (i: number) => paddingL + i * step

  const polylines = SERIES.map((s) => ({
    ...s,
    visible: !hidden.has(s.key),
    points: (history.series[s.key] as number[])
      .map((v, i) => `${xFor(i).toFixed(1)},${yFor(v).toFixed(1)}`)
      .join(' '),
  }))

  const xTicks = dates
    .map((d, i) => ({ i, label: d.slice(5) }))
    .filter((_, i) => i === 0 || i === N - 1 || (i + 1) % 5 === 0)

  return (
    <div className="h-full rounded-md border border-border bg-background p-2">
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="w-full"
        preserveAspectRatio="none"
        style={{ height: '100%' }}
      >
        {/* y 轴网格 */}
        {[0, 0.25, 0.5, 0.75, 1].map((f) => {
          const v = Math.round(yTop * f)
          const y = paddingT + (1 - f) * innerH
          return (
            <g key={f}>
              <line
                x1={paddingL}
                x2={w - paddingR}
                y1={y}
                y2={y}
                stroke="currentColor"
                strokeOpacity={0.1}
                strokeWidth={1}
              />
              <text
                x={paddingL - 4}
                y={y + 3}
                fontSize={9}
                textAnchor="end"
                fill="currentColor"
                fillOpacity={0.4}
              >
                {v}
              </text>
            </g>
          )
        })}
        {/* x 轴刻度 */}
        {xTicks.map((t) => (
          <text
            key={t.i}
            x={xFor(t.i)}
            y={h - 4}
            fontSize={9}
            textAnchor="middle"
            fill="currentColor"
            fillOpacity={0.4}
          >
            {t.label}
          </text>
        ))}
        {/* 5 折线 */}
        {polylines.map((p) =>
          p.visible ? (
            <polyline
              key={p.key}
              points={p.points}
              fill="none"
              stroke={p.color}
              strokeWidth={p.key === 'limit_up' ? 2 : 1.4}
              strokeDasharray={p.dashed ? '4 3' : undefined}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          ) : null,
        )}
        {/* 每条线最新点圆圈 */}
        {polylines.map((p) =>
          p.visible ? (
            <circle
              key={`${p.key}-dot`}
              cx={xFor(N - 1)}
              cy={yFor((history.series[p.key] as number[])[N - 1])}
              r={p.key === 'limit_up' ? 3 : 2}
              fill={p.color}
              stroke="#0f172a"
              strokeWidth={1}
            />
          ) : null,
        )}
      </svg>
    </div>
  )
}

// === Latest Card (最近交易日概览, 可指定日期 — 日期可点击弹日历) ===
export function EmotionTimelineLatestCard({
  history,
  title = '最近交易日',
  selectedDate = null,
  onDateChange,
  maxDate,
}: {
  history: HistorySeries
  title?: string
  /** 指定日期 (YYYY-MM-DD). 找到则显示该日; 找不到或 null 则 fallback 到最近非空日. */
  selectedDate?: string | null
  /** 日期变更回调 — 接 DatePicker 选择 */
  onDateChange?: (v: string) => void
  /** DatePicker 限制最大可选日期 (YYYY-MM-DD) */
  maxDate?: string
}) {
  const dayForDate = history.per_day.find((d) => d.date === selectedDate)
  const display = dayForDate ?? findLatestNonEmpty(history)
  return (
    <div className="h-full rounded-md border border-rose-500/20 bg-rose-500/5 p-3">
      <div className="flex items-baseline justify-between">
        <span className="text-xs text-muted">{title}</span>
        {/* 日期作为 DatePicker trigger — 点日期弹日历, 选中后回调 onDateChange */}
        {onDateChange ? (
          <DatePicker
            value={selectedDate ?? display?.date ?? ''}
            onChange={onDateChange}
            placeholder="最新"
            max={maxDate}
          />
        ) : (
          <span className="text-lg font-semibold tabular-nums text-foreground">
            {display?.date ?? '--'}
          </span>
        )}
      </div>
      {display ? (
        <div className="mt-2 grid grid-cols-2 gap-1.5 text-xs">
          <Metric label="涨停" value={display.limit_up} cls="text-rose-400" />
          <Metric label="跌停" value={display.limit_down} cls="text-emerald-400" />
          <Metric label="破板" value={display.broken} cls="text-purple-400" />
          <Metric label="翘板" value={display.recovery} cls="text-yellow-400" />
          <Metric label="一字板" value={display.is_one_word} cls="text-blue-400" />
          <Metric label="最高板" value={display.max_board} cls="text-violet-400" />
        </div>
      ) : (
        <p className="mt-2 text-xs text-muted">近期无数据</p>
      )}
    </div>
  )
}

// === Legend (含 title + 元信息 + 曲线 toggle + 可选历史天数 select) ===
export function EmotionTimelineLegend({
  history,
  hidden,
  onToggle,
  historyDays,
  onHistoryDaysChange,
}: {
  history: HistorySeries
  hidden: Set<string>
  onToggle: (key: string) => void
  /** 历史天数 select 控件 (可选 — 不传则不渲染) */
  historyDays?: number
  onHistoryDaysChange?: (v: number) => void
}) {
  const N = history.dates.length
  return (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <div className="flex items-center gap-2">
        <Activity className="h-4 w-4 text-muted" />
        <h2 className="text-sm font-medium text-foreground">
          市场级日快照 · {N} 个交易日时间轴
        </h2>
        <span className="text-xs text-muted">
          ({history.dates[0]} → {history.dates[N - 1]} · 跨 {history.calendar_days_span} 个日历日 · 跳过 {history.skipped_non_trading} 个非交易日)
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {historyDays !== undefined && onHistoryDaysChange && (
          <div className="flex items-center gap-1">
            <span className="text-xs text-muted">历史天数</span>
            <select
              value={historyDays}
              onChange={(e) => onHistoryDaysChange(Number(e.target.value))}
              className="rounded-md border border-border bg-elevated px-2 py-0.5 text-xs text-foreground"
              title="历史交易日数 (含结束日)"
            >
              <option value={20}>20 日</option>
              <option value={45}>45 日</option>
              <option value={60}>60 日</option>
              <option value={90}>90 日</option>
              <option value={120}>120 日</option>
            </select>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-1.5">
          {SERIES.map((s) => {
            const visible = !hidden.has(s.key)
            return (
              <button
                key={s.key}
                type="button"
                onClick={() => onToggle(s.key)}
                className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs transition ${
                  visible
                    ? 'bg-elevated/40 text-foreground'
                    : 'text-muted opacity-50 line-through'
                }`}
                title={`${visible ? '隐藏' : '显示'} ${s.label}`}
              >
                <span
                  className="inline-block h-2 w-4 rounded-sm"
                  style={{
                    background: s.color,
                    backgroundImage: s.dashed
                      ? `repeating-linear-gradient(90deg, ${s.color} 0 4px, transparent 4px 7px)`
                      : 'none',
                  }}
                />
                {s.label}
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// === Full Layout (向后兼容 mock 模式) ===
export function EmotionTimeline({ history }: { history: HistorySeries | null }) {
  const { hidden, toggle } = useSeriesVisibility()

  if (!history || !history.dates.length) {
    return (
      <div className="rounded-lg border border-border bg-surface p-4">
        <div className="flex items-center gap-2 text-sm text-muted">
          <Activity className="h-4 w-4" />
          <span>历史时间轴 · 暂无数据</span>
        </div>
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <EmotionTimelineLegend history={history} hidden={hidden} onToggle={toggle} />
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[180px_1fr]">
        <EmotionTimelineLatestCard history={history} />
        <EmotionTimelineChart history={history} hidden={hidden} />
      </div>
    </div>
  )
}

function Metric({ label, value, cls }: { label: string; value: number; cls: string }) {
  return (
    <div className="rounded border border-border bg-background px-2 py-1">
      <div className="text-[10px] text-muted">{label}</div>
      <div className={`mt-0.5 text-sm tabular-nums ${cls}`}>{value}</div>
    </div>
  )
}

export function EmotionTimelineLoading() {
  return (
    <div className="flex h-[200px] items-center justify-center rounded-lg border border-border bg-surface">
      <Loader2 className="h-4 w-4 animate-spin text-muted" />
      <span className="ml-2 text-sm text-muted">拉取历史 20 日数据…</span>
    </div>
  )
}
