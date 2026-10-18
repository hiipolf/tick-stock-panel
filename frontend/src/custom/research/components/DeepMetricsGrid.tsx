/**
 * 6 张深度指标卡 — P2 v2.0a。
 * 净室重写: 算法见 IRP-SHORT-TERM-SPEC-v2.0.md §2.3
 */
import { pct } from './Primitives'
import { Gauge, TrendingDown, TrendingUp, Target, AlertTriangle } from 'lucide-react'

interface Props {
  metrics: {
    final_explode_rate: number
    prev_limit_up_feedback: number
    prev_consecutive_feedback: number
    promotion_by_board_overall: number
    main_line_concentration: number
    high_risk_score: number
  }
}

function riskColor(score: number): 'up' | 'down' | 'neutral' {
  if (score >= 70) return 'down'
  if (score >= 40) return 'neutral'
  return 'up'
}

export function DeepMetricsGrid({ metrics }: Props) {
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
      <div className="rounded-lg border border-border bg-surface px-4 py-3">
        <div className="flex items-center gap-1 text-xs uppercase tracking-wider text-muted">
          <Gauge className="h-3 w-3" />
          最终炸板率
        </div>
        <div className="mt-1 text-2xl font-semibold tabular-nums text-foreground">
          {pct(metrics.final_explode_rate)}
        </div>
      </div>

      <div className="rounded-lg border border-border bg-surface px-4 py-3">
        <div className="flex items-center gap-1 text-xs uppercase tracking-wider text-muted">
          <TrendingDown className="h-3 w-3" />
          昨日涨停反馈
        </div>
        <div className={`mt-1 text-2xl font-semibold tabular-nums ${metrics.prev_limit_up_feedback >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
          {pct(metrics.prev_limit_up_feedback, 2)}
        </div>
      </div>

      <div className="rounded-lg border border-border bg-surface px-4 py-3">
        <div className="flex items-center gap-1 text-xs uppercase tracking-wider text-muted">
          <TrendingDown className="h-3 w-3" />
          昨日连板反馈
        </div>
        <div className={`mt-1 text-2xl font-semibold tabular-nums ${metrics.prev_consecutive_feedback >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
          {pct(metrics.prev_consecutive_feedback, 2)}
        </div>
      </div>

      <div className="rounded-lg border border-border bg-surface px-4 py-3">
        <div className="flex items-center gap-1 text-xs uppercase tracking-wider text-muted">
          <TrendingUp className="h-3 w-3" />
          连板晋级率
        </div>
        <div className="mt-1 text-2xl font-semibold tabular-nums text-emerald-500">
          {pct(metrics.promotion_by_board_overall)}
        </div>
      </div>

      <div className="rounded-lg border border-border bg-surface px-4 py-3">
        <div className="flex items-center gap-1 text-xs uppercase tracking-wider text-muted">
          <Target className="h-3 w-3" />
          主线集中度
        </div>
        <div className="mt-1 text-2xl font-semibold tabular-nums text-foreground">
          {pct(metrics.main_line_concentration)}
        </div>
      </div>

      <div className="rounded-lg border border-border bg-surface px-4 py-3">
        <div className="flex items-center gap-1 text-xs uppercase tracking-wider text-muted">
          <AlertTriangle className="h-3 w-3" />
          收盘高位风险
        </div>
        <div
          className={`mt-1 text-2xl font-semibold tabular-nums ${
            riskColor(metrics.high_risk_score) === 'down'
              ? 'text-red-500'
              : riskColor(metrics.high_risk_score) === 'neutral'
                ? 'text-amber-500'
                : 'text-emerald-500'
          }`}
        >
          {metrics.high_risk_score} / 100
        </div>
      </div>
    </div>
  )
}