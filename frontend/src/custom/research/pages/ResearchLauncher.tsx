/**
 * ResearchLauncher — P1.3 模式切换容器。
 *
 * 在 /research 主页内用 tabs 切换 3 种模式 (默认 short-term):
 *   1. 短线连板 (ShortTermWorkspace)
 *   2. 趋势题材 (ThemeRadarWorkspace)
 *   3. AI 助手 (AssistantHint) — P4 评估, 当前为占位说明卡
 *
 * 子路由 /research/short-term 与 /research/theme-radar 仍直接渲染对应 workspace
 * (P1.2 保留, 用户可分享书签, 不被 tabs 容器包装)。
 */
import { useState } from 'react'
import { Sparkles, Activity, TrendingUp, Bot, Database } from 'lucide-react'
import { cn } from '@/lib/cn'
import ShortTermWorkspace from './ShortTermWorkspace'
import ThemeRadarWorkspace from './ThemeRadarWorkspace'
import type { LucideIcon } from 'lucide-react'

type Mode = 'short-term' | 'theme-radar' | 'assistant'

interface ModeTab {
  id: Mode
  label: string
  icon: LucideIcon
  hint: string
}

const MODES: ModeTab[] = [
  { id: 'short-term', label: '短线连板', icon: Activity, hint: '涨停池 + 连板梯队 + 晋级率' },
  { id: 'theme-radar', label: '趋势题材', icon: TrendingUp, hint: '题材排名 + 等权指数 + 产业链' },
  { id: 'assistant', label: 'AI 助手', icon: Bot, hint: 'P4 评估 — TSP 扩展契约 v1 不支持工具注入' },
]

function AssistantHint({ onJumpMode }: { onJumpMode: (m: Mode) => void }) {
  return (
    <div className="flex h-full flex-col gap-4 p-6">
      <header>
        <h1 className="text-2xl font-semibold text-foreground">AI 助手集成</h1>
        <p className="mt-1 text-sm text-muted">P4 评估 — 当前未启用</p>
      </header>

      <section className="rounded-lg border border-border bg-surface p-5">
        <div className="flex items-start gap-3">
          <Bot className="mt-0.5 h-5 w-5 text-muted" />
          <div>
            <h2 className="text-base font-medium text-foreground">现状</h2>
            <p className="mt-1 text-sm text-secondary">
              IRP 的 4 个工具（涨停池 / 连板梯队 / 题材指数 K 线 / 产业链归因）已通过
              <code className="mx-1 rounded bg-background px-1.5 py-0.5 font-mono text-xs">
                /api/custom/research/*
              </code>
              端点暴露。当前可直接被前端 fetch 调用。
            </p>
          </div>
        </div>
      </section>

      <section className="rounded-lg border border-border bg-surface p-5">
        <h2 className="text-base font-medium text-foreground">TSP 助手集成的 3 个选项</h2>
        <p className="mt-1 text-sm text-secondary">
          TSP v1 扩展契约 <code className="font-mono text-xs">BackendExtensionRegistrar</code>
          只暴露 <code className="font-mono text-xs">include_router</code> +
          <code className="font-mono text-xs">register_notification_formatter</code>，
          <strong className="text-rose-400"> 没有 register_tool </strong>
          （按 docs/secondary-development.md「按需扩展」原则, 上游明确"不预埋通用框架"）。
        </p>
        <ol className="mt-3 space-y-3 text-sm text-secondary">
          <li className="rounded-md border border-border bg-background p-3">
            <div className="font-medium text-foreground">A. 给 TSP 上游提 PR 加 <code className="font-mono text-xs">register_tool</code>（推荐）</div>
            <p className="mt-1 text-xs text-muted">
              影响 1 处核心 + 1 处测试 + docs 更新。上游能采纳, 后续所有二开都受益。
              <br />
              缺点: 跨仓库 PR 流程, 等待合并时间长 (数天到数周)。
            </p>
          </li>
          <li className="rounded-md border border-border bg-background p-3">
            <div className="font-medium text-foreground">B. IRP 自建独立 chat 端点</div>
            <p className="mt-1 text-xs text-muted">
              写 <code className="font-mono text-xs">/api/custom/research/chat</code> SSE,
              独立 AI provider, 独立 system prompt。不动 TSP 核心。
              <br />
              缺点: 用户多一个 chat 入口, 跟 TSP 助手样式割裂。
            </p>
          </li>
          <li className="rounded-md border border-border bg-background p-3">
            <div className="font-medium text-foreground">C. 当前方案：HTTP 端点 + 前端 fetch</div>
            <p className="mt-1 text-xs text-muted">
              4 工具已暴露 HTTP API, 前端任何页面（含助手抽屉）可直接调。
              <br />
              缺点: 无法让 LLM 自动调用 (用户需手动选择 / 复制粘贴参数)。
            </p>
          </li>
        </ol>
      </section>

      <section className="rounded-lg border border-border bg-surface p-5">
        <h2 className="text-base font-medium text-foreground">推荐路径</h2>
        <p className="mt-1 text-sm text-secondary">
          先保持 C 方案（已实现）。等用户对 AI 助手使用频次 ≥ 5 次/周 后, 再做 A 方案提 PR。
          B 方案仅在 C 不够用时启动。
        </p>
        <div className="mt-4 flex gap-2">
          <button
            type="button"
            onClick={() => onJumpMode('short-term')}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground transition hover:bg-surface-hover"
          >
            <Activity className="h-3.5 w-3.5" />
            去看涨停池
          </button>
          <button
            type="button"
            onClick={() => onJumpMode('theme-radar')}
            className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground transition hover:bg-surface-hover"
          >
            <TrendingUp className="h-3.5 w-3.5" />
            去看题材雷达
          </button>
        </div>
      </section>
    </div>
  )
}

export default function ResearchLauncher() {
  const [mode, setMode] = useState<Mode>('short-term')
  const [dataSource, setDataSource] = useState<'mock' | 'api'>('mock')

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-border bg-surface px-6 py-3">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h1 className="flex items-center gap-2 text-lg font-semibold text-foreground">
              <Sparkles className="h-4 w-4 text-accent" />
              研究工作台
            </h1>
            <p className="mt-0.5 text-xs text-muted">
              {MODES.find((m) => m.id === mode)?.hint}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1 text-xs text-muted">
              <Database className="h-3 w-3" />
              mock
            </span>
            <button
              type="button"
              onClick={() => setDataSource(dataSource === 'mock' ? 'api' : 'mock')}
              className="rounded-md border border-border bg-background px-2.5 py-1 text-xs text-secondary transition hover:bg-surface-hover"
              title="P2/P3 将切换到真实数据"
            >
              切换到 {dataSource === 'mock' ? '后端' : 'mock'} (P2/P3)
            </button>
          </div>
        </div>

        <nav className="mt-3 flex gap-1" role="tablist">
          {MODES.map((m) => {
            const Icon = m.icon
            const active = m.id === mode
            return (
              <button
                key={m.id}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => setMode(m.id)}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm transition',
                  active
                    ? 'border-accent/40 bg-accent/10 text-foreground'
                    : 'border-transparent text-secondary hover:bg-surface-hover',
                )}
              >
                <Icon className="h-3.5 w-3.5" />
                {m.label}
              </button>
            )
          })}
        </nav>
      </header>

      <main className="flex-1 overflow-auto">
        {mode === 'short-term' && <ShortTermWorkspace embedded />}
        {mode === 'theme-radar' && <ThemeRadarWorkspace embedded />}
        {mode === 'assistant' && <AssistantHint onJumpMode={setMode} />}
      </main>
    </div>
  )
}