/**
 * IRP API client — P2 v2.0b (mock + 后端双源).
 */
import type { MockQuote } from './mock'

const BASE = '/api/custom/research'

export interface LimitUpPoolResult {
  trade_date: string
  items: any[]
  ladder: any[]
  stats: {
    limit_up_total: number
    limit_down_total: number
    exploded_total: number
    max_board: number
    seal_rate: number
    exploded_rate: number
    ladder_count: number
    promotion_rate: { by_board: Record<string, number>; overall: number }
  }
}

// P2 v2.0b: 聚合端点响应
export interface ShortTermOverview {
  trade_date: string
  emotion_score: number
  phase: string
  secondary_indicators: {
    high_pos_avg: number
    decline_coverage: number
    high_collapse: number
    high_promotion: number
  }
  deep_metrics: {
    final_explode_rate: number
    prev_limit_up_feedback: number
    prev_consecutive_feedback: number
    promotion_by_board_overall: number
    main_line_concentration: number
    high_risk_score: number
  }
  promotion_by_board: { from_board: number; to_label: string; promoted: number; total: number; ratio: number }[]
  theme_heat: { theme_id: string; theme_name: string; max_board: number; limit_up_count: number; consecutive_count: number; heat_score: number }[]
  items: any[]
  ladder: any[]
  stats: {
    limit_up_total: number
    limit_down_total: number
    exploded_total: number
    re_seal_count: number
    max_board: number
    seal_rate: number
    ladder_count: number
  }
}

export interface ThemeIndexKlineResult {
  theme_id: string
  freq: string
  bars: { date: string; open: number; high: number; low: number; close: number; volume: number; amount: number }[]
}

export interface ThemeAttributionResult {
  theme_id: string
  theme_name: string
  industries: string[]
  attribution: { node: string; weight: number; avg_change_pct: number; contribution: number; stocks: { symbol: string; change_pct: number }[] }[]
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json; charset=utf-8' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`)
  }
  const json = await res.json()
  if (json && typeof json === 'object' && 'ok' in json) {
    if (!json.ok) throw new Error(json.error ?? 'unknown error')
    return json.result as T
  }
  return json as T
}

export async function fetchLimitUpPool(
  tradeDate: string,
  quotes: MockQuote[],
  prevPool: { symbol: string; board_count: number }[] | null,
): Promise<LimitUpPoolResult> {
  return postJSON<LimitUpPoolResult>('/limit-up-pool', {
    trade_date: tradeDate,
    quotes,
    prev_pool: prevPool,
  })
}

export async function fetchThemeIndexKline(
  themeId: string,
  freq: '1d' | '1w' | '1m',
  stocks: Record<string, { date: string; close: number; volume?: number; amount?: number }[]>,
): Promise<ThemeIndexKlineResult> {
  return postJSON<ThemeIndexKlineResult>('/theme-index-kline', {
    theme_id: themeId,
    freq,
    stocks,
  })
}

export async function fetchThemeAttribution(
  themeId: string,
  changePctMap: Record<string, number>,
): Promise<ThemeAttributionResult> {
  return postJSON<ThemeAttributionResult>('/theme-attribution', {
    theme_id: themeId,
    change_pct_map: changePctMap,
  })
}

export async function fetchTools(): Promise<{ tools: { function: { name: string } }[] }> {
  const res = await fetch(`${BASE}/tools`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

export async function fetchStatus(): Promise<{
  id: string
  phase: string
  tools: string[]
}> {
  const res = await fetch(`${BASE}/status`)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return res.json()
}

// P2 v2.0b: 聚合端点 (短路版 — caller 传 quotes)
export async function fetchShortTermOverview(args: {
  trade_date: string
  quotes: MockQuote[]
  prev_pool: { symbol: string; board_count: number }[] | null
  exploded_count: number
  history_30d: any[]
}): Promise<ShortTermOverview> {
  return postJSON<ShortTermOverview>('/short-term-overview', {
    trade_date: args.trade_date,
    quotes: args.quotes,
    prev_pool: args.prev_pool,
    exploded_count: args.exploded_count,
    history_30d: args.history_30d,
  })
}

// P2 v2.0c: 真实数据接入 — 后端自调 TSP screener 拉数据, 调 6 算法
// GET 端点, 无需 caller 传 quotes; result 含 ladder_raw (limit-ladder 完整 tiers
// 含 status/is_one_word/sealed_status/ext 概念行业), 供前端卡片渲染
export async function fetchShortTermReal(asOf?: string): Promise<{
  result: ShortTermOverview & {
    ladder_raw?: {
      as_of: string
      counts?: { up: number; down: number }
      tiers: { boards: number; count: number; stocks: any[] }[]
    }
  }
  source: {
    ladder_url: string
    snapshot_url: string
    quote_count: number
    trade_date: string
    prev_pool_size: number
    ext_columns?: string | null
    as_of_requested?: string | null
  }
}> {
  const url = asOf
    ? `${BASE}/short-term-real?as_of=${encodeURIComponent(asOf)}`
    : `${BASE}/short-term-real`
  const res = await fetch(url)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`)
  }
  const json = await res.json()
  if (!json.ok) throw new Error(json.detail ?? 'unknown error')
  return json
}

// P2 v2.0f: 20 日历史 (5 曲线时间轴)
// 返回过去 N 天每日 stats: limit_up / broken / failed / recovery / is_one_word
// / limit_up_main / limit_up_star / limit_up_chinext / limit_up_bj / max_board
export interface HistorySeries {
  end_date: string
  /** 请求的交易日数 (用户传入的 days) */
  days: number
  /** 实际返回的有效交易日数 (排除周末/节假日后) */
  trading_days_returned: number
  /** 跨多少个日历日 (含跳过的非交易日) */
  calendar_days_span: number
  /** 跳过的非交易日数 */
  skipped_non_trading: number
  dates: string[]
  series: {
    limit_up: number[]
    limit_down: number[]
    broken: number[]
    failed: number[]
    recovery: number[]
    is_one_word: number[]
    max_board: number[]
    limit_up_main: number[]
    limit_up_star: number[]
    limit_up_chinext: number[]
    limit_up_bj: number[]
  }
  per_day: Array<{
    date: string
    limit_up: number
    limit_down: number
    broken: number
    failed: number
    recovery: number
    is_one_word: number
    max_board: number
    limit_up_main: number
    limit_up_star: number
    limit_up_chinext: number
    limit_up_bj: number
    ok: boolean
    is_trading_day?: boolean
  }>
}

export async function fetchShortTermHistory(
  asOf?: string,
  days = 20,
): Promise<{ result: HistorySeries; source: { ladder_url: string; as_of_requested?: string | null } }> {
  const params = new URLSearchParams({ days: String(days) })
  if (asOf) params.set('as_of', asOf)
  const res = await fetch(`${BASE}/short-term-history?${params}`)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`)
  }
  const json = await res.json()
  if (!json.ok) throw new Error(json.detail ?? 'unknown error')
  return json
}