/**
 * 概念热度榜 (5 维加权) — P2 v2.0a。
 * 净室重写: 公式见 IRP-SHORT-TERM-SPEC-v2.0.md §2.5
 */
import { TrendingUp } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { MockThemeHeat } from '../mocks/mock'

export function ThemeHeatList({ items }: { items: MockThemeHeat[] }) {
  if (!items.length) return null
  const maxScore = Math.max(...items.map((i) => i.heat_score), 1)
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <div className="mb-3 flex items-center gap-2">
        <TrendingUp className="h-4 w-4 text-muted" />
        <h2 className="text-sm font-medium text-foreground">涨停与连板聚类 · 炒作概念热度</h2>
      </div>
      <div className="space-y-1.5">
        {items.map((t) => {
          const color =
            t.heat_score >= 80
              ? 'bg-sky-500'
              : t.heat_score >= 50
                ? 'bg-sky-400'
                : t.heat_score >= 30
                  ? 'bg-sky-300'
                  : 'bg-slate-400'
          return (
            <div
              key={t.theme_id}
              className="grid grid-cols-[1fr_140px_1fr_50px] items-center gap-3 rounded-md border border-border bg-background px-3 py-2 text-sm"
            >
              <div className="font-medium text-foreground">{t.theme_name}</div>
              <div className="text-xs tabular-nums text-muted">
                {t.max_board} 板 / {t.limit_up_count} 只 / 连板 {t.consecutive_count} 只
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-border">
                <div
                  className={cn('h-full transition-all', color)}
                  style={{ width: `${(t.heat_score / maxScore) * 100}%` }}
                />
              </div>
              <div className="text-right tabular-nums text-foreground">{t.heat_score}</div>
            </div>
          )
        })}
      </div>
      <p className="mt-3 text-xs text-muted">
        热度由涨停广度、连板数量、高度、昨日延续和概念宽泛度共同计算; 宽泛标签会降权。
      </p>
    </div>
  )
}