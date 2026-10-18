/**
 * 数据存储面板 — 展示所有数据源 / 扩展数据 / 存储用量 / 调度的当前状态。
 *
 * 数据源元信息从 /api/settings/data-sources 拉(builtin / plugins / custom 三组);
 * 存储与调度从 /api/data/status 拉(60s TTL); 扩展数据从 /api/ext-data 拉。
 * 三套数据源 / 大小 / 周期 / 状态合并成一张统一的"数据资产面板"。
 */
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Database,
  HardDrive,
  Radio,
  RefreshCw,
  Clock,
  CheckCircle2,
  AlertCircle,
  CircleSlash,
  Plug,
  FileBox,
  Calendar,
  Activity,
} from 'lucide-react'
import { api, type DataSourceItem, type PluginDataSourceItem, type ExtDataConfig } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useInvalidateTierRelated } from './Keys'
import { cn } from '@/lib/cn'

// 数据集代号 → 中文 + 图标
const DATASET_LABEL: Record<string, string> = {
  realtime: '实时', daily: '日K', minute: '分钟', full_minute: '全量分钟',
  adj_factor: '除权', depth5: '五档', financial: '财务',
}
const DATASET_ICON: Record<string, typeof Radio> = {
  realtime: Radio, daily: Database, minute: Clock, full_minute: Activity,
  adj_factor: HardDrive, depth5: FileBox, financial: FileBox,
}

// 数据源类型标签
const KIND_LABEL: Record<string, string> = {
  builtin: '内置', plugin: '插件', custom: '自定义',
}
const KIND_STYLE: Record<string, string> = {
  builtin: 'bg-accent/10 text-accent border-accent/20',
  plugin: 'bg-info/10 text-info border-info/20',
  custom: 'bg-warning/10 text-warning border-warning/20',
}

// 周期格式 (ext_data schedule_minutes 可能是 1/5/60/1440 等)
function fmtSchedule(min: number | null | undefined): string {
  if (min == null) return '—'
  if (min < 60) return `${min} 分钟`
  if (min < 1440) return `${(min / 60).toFixed(min % 60 ? 1 : 0)} 小时`
  return `${(min / 1440).toFixed(min % 1440 ? 1 : 0)} 天`
}

// 字节数 → 易读
function fmtSize(mb: number | null | undefined): string {
  if (mb == null) return '—'
  if (mb < 1) return `${(mb * 1024).toFixed(0)} KB`
  if (mb < 1024) return `${mb.toFixed(1)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}

// ISO 时间 → 北京时间 (HH:MM:SS 或 MM-DD HH:MM)
function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return iso
    const pad = (n: number) => n.toString().padStart(2, '0')
    const now = new Date()
    const sameDay = d.toDateString() === now.toDateString()
    if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  } catch {
    return iso
  }
}

// ===== 通用 section 标题 =====
function SectionHeading({ icon: Icon, title, badge, right }: {
  icon: React.ComponentType<{ className?: string }>
  title: string
  badge?: string
  right?: React.ReactNode
}) {
  return (
    <div className="flex items-center justify-between gap-2 mb-3">
      <div className="flex items-center gap-2 min-w-0">
        <Icon className="h-3.5 w-3.5 text-secondary shrink-0" />
        <h3 className="text-xs font-medium text-foreground">{title}</h3>
        {badge && (
          <span className="px-1.5 py-0.5 text-[10px] font-mono rounded bg-elevated text-muted shrink-0">{badge}</span>
        )}
      </div>
      {right}
    </div>
  )
}

// ===== 数据源表格行 =====
type AnySource = (DataSourceItem | PluginDataSourceItem) & { kind: 'builtin' | 'plugin' | 'custom' }

function SourceRow({ src }: { src: AnySource }) {
  const datasets = src.datasets ?? []
  const status = (src as PluginDataSourceItem).status ?? ''
  const available = (src as PluginDataSourceItem).available ?? true
  return (
    <tr className="border-b border-border/50 last:border-b-0 hover:bg-elevated/30 transition-colors">
      <td className="py-2.5 pl-3 pr-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className={cn(
            'inline-flex h-[18px] items-center rounded px-1.5 text-[9px] font-mono font-bold uppercase border shrink-0',
            KIND_STYLE[src.kind],
          )}>
            {KIND_LABEL[src.kind]}
          </span>
          <span className="text-xs font-medium text-foreground truncate">{src.display_name}</span>
        </div>
      </td>
      <td className="py-2.5 px-2">
        <div className="flex flex-wrap gap-1">
          {datasets.length === 0 ? (
            <span className="text-[10px] text-muted">无</span>
          ) : datasets.map(d => {
            const Icon = DATASET_ICON[d] ?? Database
            return (
              <span key={d} className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-elevated/60 text-[10px] text-secondary">
                <Icon className="h-2.5 w-2.5" />
                {DATASET_LABEL[d] ?? d}
              </span>
            )
          })}
        </div>
      </td>
      <td className="py-2.5 px-2">
        <span className="text-[10px] text-muted font-mono">{src.name}</span>
      </td>
      <td className="py-2.5 pr-3 pl-2 text-right">
        {available ? (
          <span className="inline-flex items-center gap-1 text-[10px] text-success">
            <CheckCircle2 className="h-3 w-3" />
            {status || 'ok'}
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-[10px] text-warning" title={status}>
            <AlertCircle className="h-3 w-3" />
            {status || '未就绪'}
          </span>
        )}
      </td>
    </tr>
  )
}

// ===== 数据源分组表格 =====
function SourceGroup({ title, items, icon: Icon }: {
  title: string
  items: AnySource[]
  icon: React.ComponentType<{ className?: string }>
}) {
  if (items.length === 0) return null
  return (
    <div className="mb-4 last:mb-0">
      <div className="flex items-center gap-1.5 mb-2 text-[10px] text-muted">
        <Icon className="h-3 w-3" />
        {title} <span className="font-mono">({items.length})</span>
      </div>
      <div className="rounded-btn border border-border overflow-hidden">
        <table className="w-full">
          <thead className="bg-elevated/40">
            <tr className="text-[10px] text-muted">
              <th className="py-1.5 pl-3 pr-2 text-left font-medium">名称</th>
              <th className="py-1.5 px-2 text-left font-medium">数据集</th>
              <th className="py-1.5 px-2 text-left font-medium">标识</th>
              <th className="py-1.5 pr-3 pl-2 text-right font-medium">状态</th>
            </tr>
          </thead>
          <tbody>
            {items.map(s => <SourceRow key={s.name} src={s} />)}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ===== 扩展数据表格行 =====
function ExtDataRow({ cfg }: { cfg: ExtDataConfig }) {
  const pull = cfg.pull
  const schedule = pull?.schedule_minutes ?? null
  const status = pull?.last_status ?? 'never'
  const lastRun = pull?.last_run ?? null
  return (
    <tr className="border-b border-border/50 last:border-b-0 hover:bg-elevated/30 transition-colors">
      <td className="py-2.5 pl-3 pr-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="inline-flex h-[18px] items-center rounded px-1.5 text-[9px] font-mono font-bold uppercase border bg-info/10 text-info border-info/20 shrink-0">
            ext
          </span>
          <span className="text-xs font-medium text-foreground truncate">{cfg.label}</span>
        </div>
      </td>
      <td className="py-2.5 px-2">
        <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-elevated/60 text-[10px] text-secondary">
          {cfg.mode === 'snapshot' ? '快照' : '时序'}
        </span>
      </td>
      <td className="py-2.5 px-2">
        <span className="text-[10px] text-secondary font-mono">{fmtSchedule(schedule)}</span>
      </td>
      <td className="py-2.5 px-2">
        <span className="text-[10px] text-muted font-mono">{fmtTime(lastRun)}</span>
      </td>
      <td className="py-2.5 pr-3 pl-2 text-right">
        {status === 'success' ? (
          <span className="inline-flex items-center gap-1 text-[10px] text-success">
            <CheckCircle2 className="h-3 w-3" /> 成功
          </span>
        ) : status === 'error' ? (
          <span className="inline-flex items-center gap-1 text-[10px] text-warning" title={pull?.last_message ?? ''}>
            <AlertCircle className="h-3 w-3" /> 失败
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-[10px] text-muted">
            <CircleSlash className="h-3 w-3" /> 未运行
          </span>
        )}
      </td>
    </tr>
  )
}

// ===== 存储用量卡 (单个 subdir) =====
function StorageCard({ label, files, sizeMb, icon: Icon }: {
  label: string; files: number; sizeMb: number
  icon: React.ComponentType<{ className?: string }>
}) {
  return (
    <div className="rounded-btn border border-border bg-elevated/30 px-3 py-2.5">
      <div className="flex items-center gap-1.5 text-[10px] text-muted mb-1">
        <Icon className="h-3 w-3" />
        {label}
      </div>
      <div className="text-base font-semibold font-mono text-foreground leading-tight">
        {fmtSize(sizeMb)}
      </div>
      <div className="text-[10px] text-muted font-mono mt-0.5">
        {files.toLocaleString()} 文件
      </div>
    </div>
  )
}

// ===== 主面板 =====
export function SettingsDataStoragePanel() {
  const invalidate = useInvalidateTierRelated()

  const sources = useQuery({
    queryKey: QK.dataSources,
    queryFn: api.dataSources,
    refetchOnWindowFocus: false,
  })
  const status = useQuery({
    queryKey: QK.dataStatus,
    queryFn: api.dataStatus,
    refetchInterval: 60_000,
  })
  const extData = useQuery({
    queryKey: QK.extData,
    queryFn: api.extDataList,
    refetchOnWindowFocus: false,
  })

  // 合并三组数据源
  const builtin: AnySource[] = (sources.data?.builtin ?? []).map(s => ({ ...s, kind: 'builtin' as const }))
  const plugins: AnySource[] = (sources.data?.plugins ?? []).map(s => ({ ...s, kind: 'plugin' as const }))
  const custom: AnySource[] = (sources.data?.custom ?? []).map(s => ({ ...s, kind: 'custom' as const }))
  const allSources = [...builtin, ...plugins, ...custom]

  // 加载 / 错误态
  const isLoading = sources.isLoading || status.isLoading || extData.isLoading
  const hasError = sources.error || status.error || extData.error
  const [refreshing, setRefreshing] = useState(false)
  useEffect(() => {
    if (sources.isFetching || status.isFetching || extData.isFetching) setRefreshing(true)
    else setRefreshing(false)
  }, [sources.isFetching, status.isFetching, extData.isFetching])

  const refreshAll = () => {
    sources.refetch()
    status.refetch()
    extData.refetch()
    invalidate()
  }

  // 关键 subdir (按用户视角的"重要程度"排序, 不全列以免噪音)
  const storage = status.data?.storage
  const cards = storage ? [
    { label: '日 K',     files: storage.daily_files,     sizeMb: storage.daily_size_mb,     icon: Database },
    { label: '日 K enriched', files: storage.enriched_files, sizeMb: storage.enriched_size_mb, icon: HardDrive },
    { label: '分钟 K',  files: storage.minute_files,    sizeMb: storage.minute_size_mb,    icon: Clock },
    { label: '除权因子', files: storage.adj_factor_files ?? 0, sizeMb: storage.adj_factor_size_mb ?? 0, icon: FileBox },
    { label: '标的',     files: storage.instruments_files ?? 0, sizeMb: storage.instruments_size_mb ?? 0, icon: Database },
    { label: '扩展数据', files: storage.ext_data_files ?? 0, sizeMb: storage.ext_data_size_mb ?? 0, icon: Plug },
    { label: '财务',     files: storage.financials_files ?? 0, sizeMb: storage.financials_size_mb ?? 0, icon: FileBox },
  ] : []

  return (
    <div className="space-y-6">
      {hasError && (
        <div className="rounded-card border border-warning/30 bg-warning/[0.04] px-4 py-3 text-xs text-warning">
          数据加载异常: {String((sources.error || status.error || extData.error))}
        </div>
      )}

      {/* ===== Section 1: 数据源 ===== */}
      <div className="rounded-card border border-border p-4">
        <SectionHeading
          icon={Plug}
          title="数据源"
          badge={`${allSources.length} 个`}
          right={
            <button
              type="button"
              onClick={refreshAll}
              className="inline-flex items-center gap-1 px-2 h-6 rounded-btn text-[10px] text-secondary hover:bg-elevated/60 transition-colors"
            >
              <RefreshCw className={cn('h-3 w-3', refreshing && 'animate-spin')} />
              刷新
            </button>
          }
        />
        {isLoading ? (
          <div className="text-[10px] text-muted py-4 text-center">加载中…</div>
        ) : (
          <SourceGroup title="内置"   icon={Database}  items={builtin} />
        )}
        <SourceGroup title="插件"   icon={Plug}      items={plugins} />
        <SourceGroup title="自定义" icon={HardDrive} items={custom} />
        {allSources.length === 0 && !isLoading && (
          <div className="text-[10px] text-muted py-4 text-center">暂无数据源</div>
        )}
      </div>

      {/* ===== Section 2: 扩展数据 ===== */}
      <div className="rounded-card border border-border p-4">
        <SectionHeading
          icon={FileBox}
          title="扩展数据"
          badge={`${extData.data?.items?.length ?? 0} 项`}
        />
        {extData.isLoading ? (
          <div className="text-[10px] text-muted py-4 text-center">加载中…</div>
        ) : (extData.data?.items?.length ?? 0) === 0 ? (
          <div className="text-[10px] text-muted py-4 text-center">
            暂无扩展数据。前往 <span className="font-mono text-secondary">设置 → 扩展页面</span> 添加 HTTP 拉取配置。
          </div>
        ) : (
          <div className="rounded-btn border border-border overflow-hidden">
            <table className="w-full">
              <thead className="bg-elevated/40">
                <tr className="text-[10px] text-muted">
                  <th className="py-1.5 pl-3 pr-2 text-left font-medium">名称</th>
                  <th className="py-1.5 px-2 text-left font-medium">类型</th>
                  <th className="py-1.5 px-2 text-left font-medium">周期</th>
                  <th className="py-1.5 px-2 text-left font-medium">上次运行</th>
                  <th className="py-1.5 pr-3 pl-2 text-right font-medium">状态</th>
                </tr>
              </thead>
              <tbody>
                <AnimatePresence>
                  {extData.data!.items.map(cfg => (
                    <motion.tr
                      key={cfg.id}
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      transition={{ duration: 0.15 }}
                    >
                      <td colSpan={5} className="p-0">
                        <table className="w-full">
                          <tbody><ExtDataRow cfg={cfg} /></tbody>
                        </table>
                      </td>
                    </motion.tr>
                  ))}
                </AnimatePresence>
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ===== Section 3: 存储概览 ===== */}
      <div className="rounded-card border border-border p-4">
        <SectionHeading
          icon={HardDrive}
          title="存储概览"
          badge={storage ? fmtSize(storage.total_size_mb) : '—'}
        />
        {status.isLoading ? (
          <div className="text-[10px] text-muted py-4 text-center">加载中…</div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2">
            {cards.map(c => <StorageCard key={c.label} {...c} />)}
          </div>
        )}
        <p className="mt-3 text-[10px] text-muted leading-relaxed">
          占用基于 <span className="font-mono text-secondary">data/</span> 子目录 parquet 实际字节数。点击数据页 → 立即同步可写入更多数据,清理数据请前往数据页。
        </p>
      </div>

      {/* ===== Section 4: 调度 ===== */}
      <div className="rounded-card border border-border p-4">
        <SectionHeading icon={Calendar} title="调度" />
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
          <div>
            <div className="text-[10px] text-muted mb-1">下次标的同步</div>
            <div className="text-foreground font-mono">{fmtTime(status.data?.next_instruments_run)}</div>
          </div>
          <div>
            <div className="text-[10px] text-muted mb-1">下次 pipeline</div>
            <div className="text-foreground font-mono">{fmtTime(status.data?.next_pipeline_run)}</div>
          </div>
          <div>
            <div className="text-[10px] text-muted mb-1">上次标的同步</div>
            <div className="text-foreground font-mono">{fmtTime(status.data?.last_instruments_run)}</div>
          </div>
          <div>
            <div className="text-[10px] text-muted mb-1">上次 pipeline</div>
            <div className="text-foreground font-mono">{fmtTime(status.data?.last_pipeline_run)}</div>
          </div>
        </div>
      </div>
    </div>
  )
}