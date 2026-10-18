/**
 * 短线连板工作台 — P2 v2.0i (用户本轮最终 UI: 卡片嵌入 DatePicker + 历史天数挪到图表标题).
 *
 * 7 区:
 *   1. 最近交易日卡片 (顶, 卡片内日期可点击弹日历 — DatePicker)
 *   2. 5 曲线情绪时间轴 (图例标题栏带"历史天数" select)
 *   3. 6 张深度指标卡 (DeepMetricsGrid)
 *   4. 4 段晋级率进度条 (PromotionByBoard)
 *   5+6. 5 张综合统计 (StatCard) — 放在 limit-ladder 上面
 *   7. 涨停池 (嵌入 limit-ladder — 全功能继承, 不再独立 DatePicker)
 *
 * 数据源:
 *   - api (默认, P2 v2.0c 上线):  GET /api/custom/research/short-term-real
 *       + GET /short-term-history?days=20 (5 曲线时间轴)
 *   - mock:  短路版 MOCK_* (fallback / 离线 demo)
 *
 * P2 v2.0i 变更:
 *   - 移除顶部右侧 "指定日期" + "历史天数" 双控件
 *   - 最近交易日卡片内的日期渲染为 DatePicker (回调 setAsOf)
 *   - 历史天数 select 移到图表 Legend (EmotionTimelineLegend.historyDays / onHistoryDaysChange)
 *   - limit-ladder 嵌入区通过 embedded prop 隐藏其原 DatePicker (统一由 IRP 顶部卡片控制 asOf)
 *
 * P2 v2.0k 变更:
 *   - 策略: 日期优先 — apiQuery 拿到 trade_date 后立刻合成 1 天 history 让 LatestCard 渲染
 *   - 之前 LatestCard gate 在 historyQuery.data (60 天 12-15s), 视觉上"上半页空下半页有数据"
 */
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { RefreshCw, Zap, Database, Activity, Loader2, AlertCircle, ExternalLink } from 'lucide-react'
import { EmptyView, StatCard } from '../components/Primitives'
import {
  EmotionTimeline,
  EmotionTimelineLoading,
  EmotionTimelineChart,
  EmotionTimelineLatestCard,
  EmotionTimelineLegend,
  useSeriesVisibility,
} from '../components/EmotionTimeline'
import { DeepMetricsGrid } from '../components/DeepMetricsGrid'
import { PromotionByBoard } from '../components/PromotionByBoard'
import { ThemeHeatList } from '../components/ThemeHeatList'
import { LimitUpLadder } from '@/custom/limit-ladder/extension'
import {
  MOCK_LIMIT_UP_ITEMS,
  MOCK_PROMOTION_SEGS,
  MOCK_STATS,
  MOCK_THEME_HEAT,
} from '../mocks/mock'
import {
  fetchShortTermReal,
  fetchShortTermHistory,
  type ShortTermOverview,
  type HistorySeries,
} from '../mocks/api'

interface Props {
  embedded?: boolean
}

export default function ShortTermWorkspace({ embedded = false }: Props = {}) {
  const [dataSource, setDataSource] = useState<'mock' | 'api'>('api')
  /** 日期选项 (YYYY-MM-DD); 空 = 最新 (后端默认). mock 模式忽略 */
  const [asOf, setAsOf] = useState('')
  /** EmotionTimeline 历史天数 (默认 60; 上限 120 受后端 single-worker 架构限制).
   * 60 天: 首次 ~12-15s / 后续 (24h cache) < 1s.
   * 90 / 120 天: 首次 30s+ (单 uvicorn worker 串行递归架构硬瓶颈), 仅按需展开. */
  const [historyDays, setHistoryDays] = useState(60)

  const apiQuery = useQuery<ShortTermOverview>({
    queryKey: ['short-term-real', asOf],
    queryFn: async () => {
      const r = await fetchShortTermReal(asOf || undefined)
      return r.result
    },
    enabled: dataSource === 'api',
    staleTime: 60_000,
    retry: 1,
  })

  // 20 日历史 (5 曲线时间轴); 用单独 query, key 含 asOf/historyDays 联动
  const historyQuery = useQuery<HistorySeries>({
    queryKey: ['short-term-history', asOf, historyDays],
    queryFn: async () => {
      const r = await fetchShortTermHistory(asOf || undefined, historyDays)
      return r.result
    },
    enabled: dataSource === 'api',
    staleTime: 5 * 60_000,
    retry: 1,
  })

  // 数据合并: mock 或 api
  const data = dataSource === 'mock' ? null : apiQuery.data
  const isLoading = dataSource === 'api' && apiQuery.isLoading
  const isError = dataSource === 'api' && apiQuery.isError

  // mock 数据
  const items = dataSource === 'mock' ? MOCK_LIMIT_UP_ITEMS : (data?.items ?? [])
  const stats = dataSource === 'mock' ? {
    limit_up_total: 12, max_board: 7, seal_rate: 1.0, ladder_count: 4,
    re_seal_count: 3, exploded_total: 3, limit_down_total: 1,
  } : (data?.stats ?? null)
  const promotionSegs = dataSource === 'mock' ? MOCK_PROMOTION_SEGS : (data?.promotion_by_board ?? [])
  const themeHeat = dataSource === 'mock' ? MOCK_THEME_HEAT : (data?.theme_heat ?? [])
  const deepMetrics = dataSource === 'mock' ? {
    final_explode_rate: MOCK_STATS.final_explode_rate,
    prev_limit_up_feedback: MOCK_STATS.prev_limit_up_feedback,
    prev_consecutive_feedback: MOCK_STATS.prev_consecutive_feedback,
    promotion_by_board_overall: MOCK_STATS.promotion_by_board_overall,
    main_line_concentration: MOCK_STATS.main_line_concentration,
    high_risk_score: MOCK_STATS.high_risk_score,
  } : (data?.deep_metrics ?? null)
  const total_seal_yi = items.reduce((s, it) => s + (it.amount ?? 0), 0) / 1e8
  const consecutive_count = items.filter((it) => (it.board_count ?? 0) >= 2).length
  const first_board_count = items.filter((it) => (it.board_count ?? 0) === 1).length
  const re_seal_count = dataSource === 'mock' ? 3 : (stats?.re_seal_count ?? 0)

  // 5 曲线时间轴的显隐状态 (在 Workspace 这一层共享, 让 Legend + Chart 同步)
  const { hidden: hiddenSeries, toggle: toggleSeries } = useSeriesVisibility()

  // max 限制到今日 (DatePicker 不让用户选未来日期)
  const todayStr = new Date().toISOString().slice(0, 10)

  // v2.0k: 日期优先策略 — LatestCard 不等 historyQuery (60 天 12-15s) 也立即渲染.
  // 合成今日 1 天 history: apiQuery.data 拿到日期 + 5 卡 stats 后, 立刻构造 1 天 per_day.
  // 当 historyQuery.data 真正回来, 切到完整 60 天 (视觉无缝, 卡片里只显示最新一天).
  // 缺 recovery 字段 (limit-ladder 不区分"翘板"), 暂用 0; is_one_word 从 ladder_raw.tiers
  // .stocks[].is_one_word 跨梯队 count.
  const historyForCard = useMemo<HistorySeries | null>(() => {
    if (historyQuery.data) return historyQuery.data
    if (!data) return null
    const is_one_word_count =
      data.ladder_raw?.tiers
        ?.flatMap((t: any) => t.stocks || [])
        ?.filter((s: any) => s.is_one_word === true).length ?? 0
    return {
      end_date: data.trade_date,
      days: 1,
      trading_days_returned: 1,
      calendar_days_span: 1,
      skipped_non_trading: 0,
      dates: [data.trade_date],
      series: {
        limit_up: [data.stats.limit_up_total],
        limit_down: [data.stats.limit_down_total],
        broken: [data.stats.exploded_total],
        failed: [0],
        recovery: [0],
        is_one_word: [is_one_word_count],
        max_board: [data.stats.max_board],
        limit_up_main: [0],
        limit_up_star: [0],
        limit_up_chinext: [0],
        limit_up_bj: [0],
      },
      per_day: [{
        date: data.trade_date,
        limit_up: data.stats.limit_up_total,
        limit_down: data.stats.limit_down_total,
        broken: data.stats.exploded_total,
        failed: 0,
        recovery: 0,
        is_one_word: is_one_word_count,
        max_board: data.stats.max_board,
        limit_up_main: 0,
        limit_up_star: 0,
        limit_up_chinext: 0,
        limit_up_bj: 0,
        ok: true,
        is_trading_day: true,
      }],
    }
  }, [data, historyQuery.data])

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      {!embedded && (
      <header className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-semibold text-foreground">短线连板</h1>
            {dataSource === 'api' && data && (
              <span className="inline-flex items-center gap-1 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-400">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                实盘 {data.trade_date}
              </span>
            )}
            {dataSource === 'mock' && (
              <span className="inline-flex items-center gap-1 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-xs text-amber-400">
                <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
                mock 短路
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-muted">
            {dataSource === 'api'
              ? data
                ? `${asOf ? `指定日期 ${asOf}` : `最新交易日 ${data.trade_date}`} · 后端 ${dataSource} 数据 · ${data.stats.limit_up_total} 只涨停 / ${data.stats.max_board} 板高标 · 历史 ${historyDays} 日`
                : '正在拉取实盘数据…'
              : '2026-10-16 · 收盘快照 · mock 短路版'}
          </p>
        </div>
        {/* v2.0i: 顶部只保留 refresh + dataSource toggle; DatePicker / historyDays 已挪到下方 */}
        <div className="flex items-center gap-2">
          {isLoading && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted" />}
          {dataSource === 'api' && (
            <button
              type="button"
              onClick={() => { apiQuery.refetch(); historyQuery.refetch() }}
              disabled={isLoading}
              className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs text-foreground transition hover:bg-surface-hover disabled:opacity-50"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
              刷新实盘
            </button>
          )}
          <button
            type="button"
            onClick={() => setDataSource(dataSource === 'mock' ? 'api' : 'mock')}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs text-foreground transition hover:bg-surface-hover"
          >
            <Database className="h-3.5 w-3.5" />
            切换到 {dataSource === 'mock' ? '实盘' : 'mock'}
          </button>
        </div>
      </header>
      )}

      {isError && (
        <div className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/5 p-3 text-xs text-rose-300">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
          <div>
            <div className="font-medium">后端拉取失败</div>
            <div className="text-rose-400/80">
              {String(apiQuery.error)}
              <button onClick={() => apiQuery.refetch()} className="ml-2 underline">重试</button>
            </div>
          </div>
        </div>
      )}

      {/* 1. v2.0i (本轮): 最近交易日卡片提到顶部.
         - 卡片内日期渲染 DatePicker (点日期弹日历), 选中后调 onDateChange → setAsOf
         - DatePicker 同步驱动: /short-term-real, 历史查询, limit-ladder 嵌入
         - 顶部右侧原本的"指定日期"DatePicker + "历史天数"select 已删, 简化 header. */}
      <section>
        {dataSource === 'mock' || !historyForCard ? (
          <div className="rounded-md border border-rose-500/20 bg-rose-500/5 p-3 text-xs text-muted">
            最近交易日卡片 · 正在拉取实盘…
          </div>
        ) : (
          <EmotionTimelineLatestCard
            history={historyForCard}
            selectedDate={asOf || null}
            onDateChange={setAsOf}
            maxDate={todayStr}
          />
        )}
      </section>

      {/* v2.0i: 2 容器 grid (左 PromotionByBoard, 右 Chart).
         Chart 的 EmotionTimelineLegend 现在带 historyDays / onHistoryDaysChange 槽,
         "历史天数" select 由这里渲染 — 跟图例标题并列, 视觉集中在同一行. */}
      <section className="grid grid-cols-1 gap-4 lg:grid-cols-[24rem_minmax(0,1fr)]">
        <div className="min-h-[280px]">
          <PromotionByBoard segs={promotionSegs} />
        </div>
        <div className="flex min-h-[280px] flex-col gap-2 rounded-lg border border-border bg-surface p-3">
          {dataSource === 'mock' ? (
            <EmotionTimeline history={null} />
          ) : historyQuery.isLoading ? (
            <EmotionTimelineLoading />
          ) : historyQuery.isError ? (
            <div className="rounded-lg border border-rose-500/30 bg-rose-500/5 p-4 text-xs text-rose-300">
              拉取 {historyDays} 日历史失败: {String(historyQuery.error)}
              <button onClick={() => historyQuery.refetch()} className="ml-2 underline">重试</button>
            </div>
          ) : historyQuery.data ? (
            <>
              <EmotionTimelineLegend
                history={historyQuery.data}
                hidden={hiddenSeries}
                onToggle={toggleSeries}
                historyDays={historyDays}
                onHistoryDaysChange={setHistoryDays}
              />
              <div className="min-h-0 flex-1">
                <EmotionTimelineChart history={historyQuery.data} hidden={hiddenSeries} />
              </div>
            </>
          ) : null}
        </div>
      </section>

      {/* 4. 概念热度榜 (独立 row, 不参与等高 grid) */}
      <section className="grid grid-cols-1">
        <ThemeHeatList items={themeHeat} />
      </section>

      {/* v2.0i (2): 5 张综合统计 + 6 张深度指标 放在 limit-ladder 嵌入上面.
         两段连续紧挨, 形成 limit-ladder 上方的"核心指标汇总"区; 视觉对齐 StatCard / DeepMetricsGrid 风格. */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-5">
        <StatCard
          label="涨停家数"
          value={stats?.limit_up_total ?? '--'}
          hint="当前无 ST 涨停"
          accent="down"
        />
        <StatCard
          label="连板家数"
          value={consecutive_count}
          hint={`首板 ${first_board_count} 只`}
          accent="up"
        />
        <StatCard
          label="最高连板"
          value={`${stats?.max_board ?? '--'} 板`}
          hint={`高标突出, 连板共 ${consecutive_count} 只`}
          accent="neutral"
        />
        <StatCard
          label="开板后回封"
          value={re_seal_count}
          hint="仅统计当前仍封板"
          accent="neutral"
        />
        <StatCard
          label="封板成交额"
          value={`${total_seal_yi.toFixed(1)} 亿`}
          hint="当前可见涨停池合计"
          accent="up"
        />
      </section>

      {deepMetrics && (
        <section>
          <DeepMetricsGrid metrics={deepMetrics} />
        </section>
      )}

      {/* 6. 连板梯队 + 涨停池 — 完整嵌入 limit-ladder 主组件, 全部能力继承.
         embedded prop 让 limit-ladder 自带 DatePicker 隐藏 (日期由上方最近交易日卡片统一控制). */}
      <section className="flex-1 rounded-lg border border-border bg-background/50 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-medium text-foreground">
            连板梯队 / 涨停池
            <span className="ml-2 text-xs text-muted">
              嵌入 limit-ladder — 涨跌停切换 / 状态筛选 / 板块筛选 / 概念行业 tag / 监控 / 封单模式 / ext 配置 全功能继承
            </span>
          </h2>
          <Link
            to="/limit-ladder"
            className="inline-flex items-center gap-1 rounded-md border border-border bg-surface px-2.5 py-1 text-xs text-foreground transition hover:bg-surface-hover"
          >
            新窗口打开
            <ExternalLink className="h-3 w-3" />
          </Link>
        </div>
        {dataSource === 'mock' ? (
          <EmptyView
            icon={Activity}
            title="mock 模式不展示 limit-ladder 嵌入"
            hint="切换到实盘数据查看完整涨停池 (涨跌停切换 / 状态筛选 / 概念行业 tag 等全部 limit-ladder 能力)"
          />
        ) : (
          <LimitUpLadder embedded asOf={asOf} onAsOfChange={setAsOf} />
        )}
      </section>

      <footer className="border-t border-border pt-3 text-xs text-muted">
        <div className="flex items-start gap-2">
          <Zap className="mt-0.5 h-3.5 w-3.5" />
          <span>
            P2 v2.0c 实盘接入：后端自调 TSP <code className="rounded bg-background px-1 font-mono text-[10px]">/api/screener/limit-ladder</code> + <code className="rounded bg-background px-1 font-mono text-[10px]">market-snapshot</code> 拉数据，调 IRP 6 算法。
            当前 <code className="rounded bg-background px-1 font-mono text-[10px]">{dataSource}</code> 数据源。
          </span>
        </div>
        <div className="mt-1 flex items-start gap-2">
          <Activity className="mt-0.5 h-3.5 w-3.5" />
          <span>
            已知限制：limit-ladder 不含 limit_time/industry/theme_tags，<code className="rounded bg-background px-1 font-mono text-[10px]">seal_rate</code> 偏 0、<code className="rounded bg-background px-1 font-mono text-[10px]">theme_heat</code> 取决于 theme_chain.json 代表股是否在涨停池。
          </span>
        </div>
      </footer>
    </div>
  )
}
