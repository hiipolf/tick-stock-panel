/**
 * 4 段晋级率进度条 — P2 v2.0a。
 */
import { ArrowUpRight } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { MockPromotionSeg } from '../mocks/mock'

export function PromotionByBoard({ segs }: { segs: MockPromotionSeg[] }) {
  if (!segs.length) return null
  return (
    <div className="rounded-lg border border-border bg-surface p-4">
      <div className="mb-3 flex items-center gap-2">
        <ArrowUpRight className="h-4 w-4 text-muted" />
        <h2 className="text-sm font-medium text-foreground">昨日 → 今日 · 封板晋级</h2>
      </div>
      <div className="space-y-2">
        {segs.map((s) => {
          const widthPct = Math.round(s.ratio * 100)
          const color =
            widthPct >= 50
              ? 'bg-sky-500'
              : widthPct >= 20
                ? 'bg-amber-500'
                : 'bg-slate-500'
          return (
            <div key={s.from_board} className="grid grid-cols-[80px_60px_1fr_80px] items-center gap-3 text-sm">
              <div className="font-medium text-foreground">{s.to_label}</div>
              <div className="tabular-nums text-muted">
                {s.promoted} / {s.total}
              </div>
              <div className="h-2.5 overflow-hidden rounded-full bg-border">
                <div
                  className={cn('h-full transition-all', color)}
                  style={{ width: `${widthPct}%` }}
                />
              </div>
              <div className="text-right tabular-nums text-foreground">{widthPct}%</div>
            </div>
          )
        })}
      </div>
      <p className="mt-3 text-xs text-muted">
        口径: 昨日非 ST 封板股, 今日继续封板且连板高度增加。盘中数据会随封板变化。
      </p>
    </div>
  )
}