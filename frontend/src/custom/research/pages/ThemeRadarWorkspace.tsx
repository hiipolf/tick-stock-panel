/**
 * IRP 趋势题材雷达工作台 — P1.2 短路版。
 *
 * 4 区域:
 *   1. 题材排名 (按 trend_score 倒序, 含阶段判定 + 当日涨跌幅)
 *   2. 选中题材的指数 K 线 (Sparkline + 列表)
 *   3. 产业链归因 (节点贡献度排序)
 *   4. 后端接入说明 (P3 替换 mock)
 *
 * 数据: P1.2 短路版使用 mock; P3 接入 TSP 真实数据。
 */
import { useMemo, useState } from 'react'
import { Database, Layers, Network, TrendingUp } from 'lucide-react'
import { EmptyView, PhaseChip, Sparkline, StatCard, pct } from '../components/Primitives'
import { MOCK_ATTRIBUTIONS, MOCK_THEMES, type MockThemeBar } from '../mocks/mock'

export default function ThemeRadarWorkspace({ embedded = false }: { embedded?: boolean } = {}) {
  const [dataSource, setDataSource] = useState<'mock' | 'api'>('mock')
  const themes = dataSource === 'mock' ? MOCK_THEMES : []
  const attributions = dataSource === 'mock' ? MOCK_ATTRIBUTIONS : {}
  const [selectedId, setSelectedId] = useState<string>('ai_chain')

  const selected = useMemo(
    () => themes.find((t) => t.theme_id === selectedId) ?? themes[0],
    [themes, selectedId],
  )
  const selectedAttr = attributions[selectedId] ?? []

  // 阶段分布统计
  const phaseCounts = useMemo(() => {
    const m: Record<string, number> = {}
    for (const t of themes) m[t.phase] = (m[t.phase] ?? 0) + 1
    return m
  }, [themes])

  return (
    <div className="flex h-full flex-col gap-4 p-6">
      {!embedded && (
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">趋势题材雷达</h1>
          <p className="mt-1 text-sm text-muted">
            题材排名 · 等权指数 K 线 · 产业链归因（数据: {dataSource === 'mock' ? 'mock' : '后端'}）
          </p>
        </div>
        <button
          type="button"
          onClick={() => setDataSource(dataSource === 'mock' ? 'api' : 'mock')}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-1.5 text-xs text-foreground transition hover:bg-surface-hover"
        >
          <Database className="h-3.5 w-3.5" />
          切换到 {dataSource === 'mock' ? '后端' : 'mock'}
        </button>
      </header>
      )}

      <section className="grid grid-cols-2 gap-3 md:grid-cols-4 lg:grid-cols-7">
        <StatCard label="覆盖题材" value={themes.length} accent="neutral" />
        <StatCard label="高潮期" value={phaseCounts['高潮'] ?? 0} accent="up" hint="顶部 10% 区间" />
        <StatCard label="启动期" value={phaseCounts['启动'] ?? 0} accent="up" hint="底部反弹 +20" />
        <StatCard label="修复期" value={phaseCounts['修复'] ?? 0} accent="neutral" hint="底部反弹 +10" />
        <StatCard label="震荡期" value={phaseCounts['震荡'] ?? 0} accent="neutral" />
        <StatCard label="退潮期" value={phaseCounts['退潮'] ?? 0} accent="down" hint="5 日高点 -15" />
        <StatCard label="冰点期" value={phaseCounts['冰点'] ?? 0} accent="down" hint="底部 10% 区间" />
      </section>

      <section className="grid flex-1 grid-cols-1 gap-4 lg:grid-cols-[1fr_1fr]">
        {/* 题材排名 */}
        <div className="rounded-lg border border-border bg-surface p-4">
          <div className="mb-3 flex items-center gap-2">
            <Layers className="h-4 w-4 text-muted" />
            <h2 className="text-sm font-medium text-foreground">题材排名</h2>
          </div>
          {themes.length === 0 ? (
            <EmptyView title="暂无题材数据" />
          ) : (
            <div className="space-y-1.5">
              {themes
                .slice()
                .sort((a, b) => b.change_pct - a.change_pct)
                .map((t, i) => (
                  <button
                    type="button"
                    key={t.theme_id}
                    onClick={() => setSelectedId(t.theme_id)}
                    className={`flex w-full items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-sm transition ${
                      selectedId === t.theme_id
                        ? 'border-accent/50 bg-accent/5'
                        : 'border-border bg-background hover:bg-surface-hover'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className="w-5 text-right text-xs tabular-nums text-muted">
                        {i + 1}
                      </span>
                      <span className="text-foreground">{t.theme_name}</span>
                    </div>
                    <div className="flex items-center gap-3">
                      <span
                        className={`tabular-nums ${t.change_pct >= 0 ? 'text-emerald-500' : 'text-red-500'}`}
                      >
                        {pct(t.change_pct)}
                      </span>
                      <PhaseChip phase={t.phase} />
                    </div>
                  </button>
                ))}
            </div>
          )}
        </div>

        {/* 选中题材详情: K 线 + 归因 */}
        <div className="flex flex-col gap-4">
          <div className="rounded-lg border border-border bg-surface p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <TrendingUp className="h-4 w-4 text-muted" />
                <h2 className="text-sm font-medium text-foreground">
                  {selected?.theme_name ?? '未选择'} · 等权指数
                </h2>
              </div>
              <span className="text-xs text-muted">1d K 线</span>
            </div>
            {selected ? (
              <>
                <Sparkline bars={selected.bars as MockThemeBar[]} />
                <div className="mt-3 grid grid-cols-3 gap-2 text-xs">
                  <div className="rounded border border-border bg-background px-2 py-1.5">
                    <div className="text-muted">起始</div>
                    <div className="mt-0.5 tabular-nums text-foreground">
                      {selected.bars[0]?.close.toFixed(2) ?? '--'}
                    </div>
                  </div>
                  <div className="rounded border border-border bg-background px-2 py-1.5">
                    <div className="text-muted">最新</div>
                    <div className="mt-0.5 tabular-nums text-foreground">
                      {selected.bars[selected.bars.length - 1]?.close.toFixed(2) ?? '--'}
                    </div>
                  </div>
                  <div className="rounded border border-border bg-background px-2 py-1.5">
                    <div className="text-muted">累计</div>
                    <div
                      className={`mt-0.5 tabular-nums ${selected.change_pct >= 0 ? 'text-emerald-500' : 'text-red-500'}`}
                    >
                      {pct(selected.change_pct)}
                    </div>
                  </div>
                </div>
              </>
            ) : (
              <EmptyView title="未选择题材" />
            )}
          </div>

          <div className="flex-1 rounded-lg border border-border bg-surface p-4">
            <div className="mb-3 flex items-center gap-2">
              <Network className="h-4 w-4 text-muted" />
              <h2 className="text-sm font-medium text-foreground">产业链归因</h2>
            </div>
            {selectedAttr.length === 0 ? (
              <EmptyView title="暂无归因数据" hint="选中题材后展示其产业链节点贡献度" />
            ) : (
              <div className="space-y-2">
                {selectedAttr
                  .slice()
                  .sort((a, b) => b.contribution - a.contribution)
                  .map((a) => (
                    <div
                      key={a.node}
                      className="rounded-md border border-border bg-background px-3 py-2"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm text-foreground">{a.node}</span>
                        <span
                          className={`tabular-nums ${a.contribution >= 0 ? 'text-emerald-500' : 'text-red-500'}`}
                        >
                          贡献 {pct(a.contribution, 3)}
                        </span>
                      </div>
                      <div className="mt-1 flex items-center justify-between text-xs text-muted">
                        <span>
                          平均涨跌 {pct(a.avg_change_pct, 2)} · 权重 {a.weight}
                        </span>
                        <span className="tabular-nums">
                          {a.stocks.map((s) => s.symbol).join(', ')}
                        </span>
                      </div>
                    </div>
                  ))}
              </div>
            )}
          </div>
        </div>
      </section>
    </div>
  )
}