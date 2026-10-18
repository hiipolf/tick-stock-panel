/**
 * IRP 通用 UI 组件 — P1.2。
 * 不依赖 TSP 内部 lib (避免不稳定契约), 用 lucide-react + tailwind utility。
 */
import { type ReactNode } from 'react'
import { Construction } from 'lucide-react'
import { cn } from '@/lib/cn'

// === Stat 卡片 ===
export function StatCard({
  label,
  value,
  hint,
  accent,
}: {
  label: string
  value: ReactNode
  hint?: string
  accent?: 'up' | 'down' | 'neutral'
}) {
  const accentColor = {
    up: 'text-emerald-500',
    down: 'text-red-500',
    neutral: 'text-foreground',
  }[accent ?? 'neutral']
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3">
      <div className="text-xs uppercase tracking-wider text-muted">{label}</div>
      <div className={cn('mt-1 text-2xl font-semibold tabular-nums', accentColor)}>
        {value}
      </div>
      {hint && <div className="mt-1 text-xs text-secondary">{hint}</div>}
    </div>
  )
}

// === 板数徽章 ===
export function BoardChip({ board }: { board: number }) {
  const color =
    board >= 5
      ? 'bg-rose-500/15 text-rose-400 ring-1 ring-rose-500/30'
      : board >= 3
        ? 'bg-amber-500/15 text-amber-400 ring-1 ring-amber-500/30'
        : 'bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30'
  const label = board >= 5 ? `${board}+` : `${board}板`
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-2 py-0.5 text-xs font-medium tabular-nums',
        color,
      )}
    >
      {label}
    </span>
  )
}

// === 阶段徽章 (5 阶段判定) ===
export function PhaseChip({ phase }: { phase: string }) {
  const color: Record<string, string> = {
    高潮: 'bg-rose-500/15 text-rose-400 ring-1 ring-rose-500/30',
    启动: 'bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30',
    修复: 'bg-sky-500/15 text-sky-400 ring-1 ring-sky-500/30',
    震荡: 'bg-slate-500/15 text-slate-400 ring-1 ring-slate-500/30',
    退潮: 'bg-orange-500/15 text-orange-400 ring-1 ring-orange-500/30',
    冰点: 'bg-indigo-500/15 text-indigo-400 ring-1 ring-indigo-500/30',
    未知: 'bg-slate-500/15 text-slate-500 ring-1 ring-slate-500/30',
  }
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-2 py-0.5 text-xs font-medium',
        color[phase] ?? color['未知'],
      )}
    >
      {phase}
    </span>
  )
}

// === 空态 (对齐 TSP EmptyState 风格) ===
export function EmptyView({
  icon: Icon = Construction,
  title,
  hint,
}: {
  icon?: typeof Construction
  title: string
  hint?: string
}) {
  return (
    <div className="grid h-full place-items-center px-8 py-16">
      <div className="max-w-md text-center">
        <Icon className="mx-auto h-10 w-10 text-muted" strokeWidth={1.5} />
        <h2 className="mt-4 text-base font-medium text-foreground">{title}</h2>
        {hint && <p className="mt-2 text-sm leading-relaxed text-secondary">{hint}</p>}
      </div>
    </div>
  )
}

// === K 线 Sparkline (轻量, 用 SVG inline) ===
export function Sparkline({ bars, width = 320, height = 80 }: { bars: { close: number }[]; width?: number; height?: number }) {
  if (!bars.length) return null
  const closes = bars.map((b) => b.close)
  const min = Math.min(...closes)
  const max = Math.max(...closes)
  const range = max - min || 1
  const step = width / (bars.length - 1 || 1)
  const points = closes
    .map((c, i) => `${(i * step).toFixed(2)},${(height - ((c - min) / range) * height).toFixed(2)}`)
    .join(' ')
  const first = closes[0]
  const last = closes[closes.length - 1]
  const trend = last >= first ? 'stroke-emerald-500' : 'stroke-red-500'
  return (
    <svg width={width} height={height} className="block">
      <polyline points={points} fill="none" strokeWidth={1.5} className={trend} />
    </svg>
  )
}

// === 百分比格式化 ===
export function pct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(Number(v))) return '--'
  const n = Number(v)
  const sign = n > 0 ? '+' : ''
  return `${sign}${(n * 100).toFixed(digits)}%`
}

export function num(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(Number(v))) return '--'
  return Number(v).toFixed(digits)
}