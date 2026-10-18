/**
 * LimitLadderStockCard — IRP 简化版涨停卡片。
 *
 * 借鉴 limit-ladder 的视觉表达 (status 渐变背景 + 板块标识 + 概念/行业 tag)
 * 但精简为 IRP 自包含组件 (不依赖 limit-ladder extension 的 framer-motion/监控菜单/
 * DatePicker/SealedBadge 等), 让 ShortTermWorkspace 也能渲染 limit-ladder 风格卡片。
 *
 * 字段映射 (来自 /short-term-real.ladder_raw.tiers[].stocks[]):
 *   - symbol, name, close, change_pct, boards (连板数)
 *   - status: 'limit_up' | 'broken' | 'failed' | 'recovery'
 *   - is_one_word: 一字板标志
 *   - sealed_status, sealed_vol: 封单修正 (limit_up 主状态展示用)
 *   - ext_gn_ths__所属概念, ext_hy_ths__所属同花顺行业: 概念/行业 tag
 *
 * 与 IRP /short-term-real 配套: 后端默认透传 ext_gn_ths/ext_hy_ths。
 */
import React from 'react'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import type { NavItem } from '@/lib/listNav'

// === 状态样式 (借鉴 limit-ladder STATUS_STYLE, 简化) ===
interface CardStyle {
  bg: string
  bar: string
  nameCls: string
  codeCls: string
  badgeText: string
  cardStyle?: React.CSSProperties
  hoverShadow?: string
}

const STATUS_STYLE: Record<string, CardStyle> = {
  limit_up: {
    bg: '',
    bar: 'border-l-2 border-bull/50',
    nameCls: 'text-rose-900 dark:text-rose-50 text-[13px]',
    codeCls: 'text-muted/80',
    badgeText: '',
    cardStyle: {
      background: 'linear-gradient(105deg, hsl(4 60% 45% / 0.14) 0%, hsl(6 50% 30% / 0.09) 40%, hsl(220 15% 12% / 0.0) 100%)',
      boxShadow: 'inset 1px 0 0 hsl(4 80% 55% / 0.12), 0 0 10px -4px hsl(4 80% 50% / 0.10)',
    },
    hoverShadow: 'inset 1px 0 0 hsl(4 80% 55% / 0.30), 0 0 18px -4px hsl(4 80% 50% / 0.28)',
  },
  broken: {
    bg: 'opacity-75',
    bar: 'border-l border-purple-400/30',
    nameCls: 'text-foreground/70 text-xs',
    codeCls: 'text-muted/60',
    badgeText: '炸',
  },
  failed: {
    bg: 'opacity-75',
    bar: 'border-l border-muted/25',
    nameCls: 'text-foreground/70 text-xs',
    codeCls: 'text-muted/60',
    badgeText: '断',
  },
  recovery: {
    bg: 'opacity-75',
    bar: 'border-l border-purple-400/30',
    nameCls: 'text-foreground/70 text-xs',
    codeCls: 'text-muted/60',
    badgeText: '撬',
  },
}

function styleFor(status: string | undefined): CardStyle {
  return STATUS_STYLE[status ?? 'limit_up'] ?? STATUS_STYLE.limit_up
}

// === 板块标识 ===
function boardTag(symbol: string): { label: string; cls: string } | null {
  if (/^(300|301)/.test(symbol)) return { label: '创', cls: 'text-[#f97316] bg-[#f97316]/12 border-[#f97316]/25' }
  if (/^688/.test(symbol))       return { label: '科', cls: 'text-cyan-400 bg-cyan-400/12 border-cyan-400/25' }
  if (/\.BJ$/.test(symbol))      return { label: '北', cls: 'text-purple-400 bg-purple-400/12 border-purple-400/25' }
  return null
}

// === 格式化 ===
function fmtPct(p: number): string {
  const sign = p >= 0 ? '+' : ''
  return `${sign}${(p * 100).toFixed(2)}%`
}

function fmtSealVol(v: number): string {
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(1) + '万'
  return v.toLocaleString()
}

// === ext tag 解析 ===
// limit-ladder 的 ext 字段以 `ext_<provider>__<field>` 为 key, value 是 ';' / '、' / ',' 分隔的字符串
function parseTags(value: unknown): string[] {
  if (value == null) return []
  const str = String(value).trim()
  if (!str) return []
  return str
    .split(/[、,，;；]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

function conceptTags(stock: LimitLadderStock): string[] {
  return parseTags((stock as Record<string, unknown>)['ext_gn_ths__所属概念'])
}

function industryTags(stock: LimitLadderStock): string[] {
  return parseTags((stock as Record<string, unknown>)['ext_hy_ths__所属同花顺行业'])
}

// === Stock 类型 (从 limit-ladder.tiers[].stocks 直接透传) ===
export interface LimitLadderStock {
  symbol: string
  name: string
  close: number
  change_pct: number
  boards: number
  status?: string
  consecutive_limit_ups?: number
  sealed_status?: string | null
  sealed_vol?: number | null
  is_one_word?: boolean
  // ext_gn_ths__所属概念 / ext_hy_ths__所属同花顺行业: 透传, parseTags 解析
  [key: string]: unknown
}

export interface LimitLadderStockCardProps {
  stock: LimitLadderStock
  /** 可选: 点击概念/行业 tag 时回调, 供父组件弹维度筛选 */
  onTagClick?: (kind: 'concept' | 'industry', tag: string) => void
  /** 可选: 当前高亮 tag (limit-ladder 同款) */
  selectedTag?: { fieldKey: 'concept' | 'industry'; tag: string } | null
  /** 可选: 提供后预览弹窗可左右切股 */
  navList?: NavItem[]
}

// === 组件本体 ===
export function LimitLadderStockCard({
  stock,
  onTagClick,
  selectedTag,
  navList,
}: LimitLadderStockCardProps) {
  const [previewOpen, setPreviewOpen] = React.useState(false)
  const style = styleFor(stock.status)
  const tag = boardTag(stock.symbol)
  const code = stock.symbol.replace(/\.BJ$/, '').replace(/\.SZ$/, '').replace(/\.SH$/, '')
  const concepts = conceptTags(stock)
  const industries = industryTags(stock)
  const isLimitHit = stock.status === 'limit_up' || stock.status === undefined

  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setPreviewOpen(true)}
        onKeyDown={(e) => {
          if (e.key !== 'Enter' && e.key !== ' ') return
          e.preventDefault()
          setPreviewOpen(true)
        }}
        className={`group w-full flex flex-col items-start gap-1 px-2.5 py-2 rounded-md transition-all duration-200 cursor-pointer hover:opacity-100 ${style.bg} ${style.bar}`}
        style={style.cardStyle}
        onMouseEnter={(e) => {
          if (!style.hoverShadow) return
          e.currentTarget.style.boxShadow = style.hoverShadow
        }}
        onMouseLeave={(e) => {
          if (!style.cardStyle) return
          e.currentTarget.style.boxShadow = style.cardStyle.boxShadow ?? ''
        }}
      >
        {/* 名称行 */}
        <div className="flex items-center gap-1.5 w-full min-w-0">
          <span className={`${style.nameCls} font-medium truncate`}>{stock.name}</span>
          {stock.is_one_word && (
            <span className="shrink-0 rounded-sm border border-bull/25 bg-bull/10 px-1 py-px text-[9px] font-medium leading-none text-bull">
              一字
            </span>
          )}
          {tag && (
            <span className={`shrink-0 text-[9px] px-1 py-px rounded-full border leading-none ${tag.cls}`}>
              {tag.label}
            </span>
          )}
        </div>

        {/* 代码 + 数字行 */}
        <div className="flex items-center gap-1.5 w-full">
          <span className={`${style.codeCls} font-mono text-[10px] tracking-tight`}>{code}</span>
          <span className="ml-auto flex items-center gap-1">
            {!isLimitHit ? (
              <span
                className={`text-[10px] font-semibold tabular-nums ${
                  stock.change_pct > 0 ? 'text-bull' : stock.change_pct < 0 ? 'text-bear' : 'text-muted'
                }`}
              >
                {fmtPct(stock.change_pct)}
              </span>
            ) : stock.sealed_status === 'real' && stock.sealed_vol != null ? (
              <span className="text-[10px] font-semibold tabular-nums text-accent/80">
                {fmtSealVol(stock.sealed_vol)}
              </span>
            ) : (
              <span className="text-[10px] font-semibold tabular-nums text-accent/80">
                {stock.boards ?? stock.consecutive_limit_ups ?? 0}
              </span>
            )}
            {style.badgeText && (
              <span className="text-[9px] font-medium text-muted">{style.badgeText}</span>
            )}
          </span>
        </div>

        {/* tag 行 */}
        {(concepts.length > 0 || industries.length > 0) && (
          <div className="flex flex-col gap-0.5 w-full">
            {concepts.length > 0 && (
              <div className="flex flex-wrap gap-0.5">
                {concepts.map((t, i) => {
                  const active = selectedTag?.fieldKey === 'concept' && selectedTag.tag === t
                  return (
                    <button
                      key={i}
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation()
                        onTagClick?.('concept', t)
                      }}
                      className={`text-[10px] leading-none px-1.5 py-0.5 rounded-sm hover:brightness-95 ${
                        active
                          ? 'text-orange-900 dark:text-orange-100 bg-orange-300/30 dark:bg-orange-400/30 ring-1 ring-orange-400/60'
                          : 'text-orange-800 bg-orange-100/80 dark:text-orange-200/60 dark:bg-orange-400/[0.05]'
                      }`}
                    >
                      {t}
                    </button>
                  )
                })}
              </div>
            )}
            {industries.length > 0 && (
              <div className="flex flex-wrap gap-0.5">
                {industries.map((t, i) => {
                  const active = selectedTag?.fieldKey === 'industry' && selectedTag.tag === t
                  return (
                    <button
                      key={i}
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation()
                        onTagClick?.('industry', t)
                      }}
                      className={`text-[10px] leading-none px-1.5 py-0.5 rounded-sm hover:brightness-95 ${
                        active
                          ? 'text-sky-900 dark:text-sky-100 bg-sky-300/30 dark:bg-sky-400/30 ring-1 ring-sky-400/60'
                          : 'text-sky-800 bg-sky-100/80 dark:text-sky-300/90 dark:bg-sky-400/10'
                      }`}
                    >
                      {t}
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        )}
      </div>
      {previewOpen && (
        <StockPreviewDialog
          symbol={stock.symbol}
          name={stock.name}
          onClose={() => setPreviewOpen(false)}
          {...(navList ? { navList } : {})}
        />
      )}
    </>
  )
}
