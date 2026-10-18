/**
 * IRP Mock 数据 — P2 v2.0a 完整版（对齐 easy-stock 短线连板 UI 密度）。
 *
 * 净室边界（用户 2026-10-16 决策）:
 *   - 字段名借鉴 easy-stock 命名习惯 (emotion_score / theme_heat / promotion_by_board)
 *   - 算法公式 IRP 净室重写（不复制 Go 源码）
 *   - 主题名 IRP 自创 (ai_chain / ev_chain / innovative_drug / solar / semiconductor)
 *
 * 后续 P2 v2.0b 把 mock 替换为真实 fetch 调用。
 */

// ============================================================
// 基础类型
// ============================================================
export interface MockQuote {
  symbol: string
  name: string
  trade_date: string
  prev_close: number
  last_close: number
  high: number
  low: number
  volume: number
  amount: number
  industry: string
  theme_tags: string[]
  limit_time: string | null
  is_exploded: boolean
  is_limit_down: boolean
  is_re_seal?: boolean
  prev_pct?: number
}

export interface MockLimitUpItem {
  symbol: string
  name: string
  trade_date: string
  limit_time: string | null
  first_limit_price: number
  last_close: number
  turnover: number
  amount: number
  industry: string
  theme_tags: string[]
  board_count: number
  is_re_seal?: boolean
  prev_pct?: number
}

export interface MockLadder {
  board: number
  count: number
  symbols: string[]
  first_limit_time: string | null
  last_limit_time: string | null
}

export interface MockThemeBar {
  date: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount: number
}

export interface MockAttribution {
  node: string
  weight: number
  avg_change_pct: number
  contribution: number
  stocks: { symbol: string; change_pct: number }[]
}

// ============================================================
// P2 v2.0 新增类型
// ============================================================

/** 情绪分数时序 (1 日 1 点) */
export interface MockEmotionBar {
  date: string
  score: number          // 0-100
  phase: '高潮' | '活跃' | '分歧' | '退潮' | '冰点'
  high_pos_avg: number   // 高位均值 (%)
  decline_coverage: number // 下跌覆盖 (0-1)
  high_collapse: number  // 高度坍缩 (板数)
  high_promotion: number // 高度晋级 (0-1)
  summary: string        // 盘后总结 (一句话)
}

/** 4 段晋级率 */
export interface MockPromotionSeg {
  from_board: number     // 1 / 2 / 3 / 5
  to_label: string       // '1进2' / '2进3' / ...
  promoted: number
  total: number
  ratio: number          // 0-1
}

/** 概念热度 */
export interface MockThemeHeat {
  theme_id: string
  theme_name: string
  max_board: number
  limit_up_count: number
  consecutive_count: number
  heat_score: number     // 0-100
}

/** 5 阶段情绪等级 */
export type EmotionPhase = '高潮' | '活跃' | '分歧' | '退潮' | '冰点'

// ============================================================
// 基础数据 (P1.2 已用, 扩展 prev_pct / is_re_seal)
// ============================================================
const today = '2026-10-16'

export const MOCK_QUOTES: MockQuote[] = [
  { symbol: '600001', name: 'A 主板 1 板-甲', trade_date: today, prev_close: 10.0, last_close: 10.97, high: 10.97, low: 10.5, volume: 12000, amount: 13164000, industry: '电子', theme_tags: ['ai_chain', 'semiconductor'], limit_time: '10:30:00', is_exploded: false, is_limit_down: false, prev_pct: 0.01 },
  { symbol: '600002', name: 'B 主板 1 板-乙', trade_date: today, prev_close: 20.0, last_close: 21.93, high: 21.93, low: 21.2, volume: 5000, amount: 10965000, industry: '通信', theme_tags: ['ai_chain'], limit_time: '13:05:00', is_exploded: false, is_limit_down: false, prev_pct: 0.02 },
  { symbol: '600003', name: 'C 主板 1 板-丙', trade_date: today, prev_close: 8.0, last_close: 8.78, high: 8.78, low: 8.5, volume: 8000, amount: 7024000, industry: '医药', theme_tags: ['innovative_drug'], limit_time: '14:00:00', is_exploded: false, is_limit_down: false, prev_pct: -0.005 },
  { symbol: '600010', name: 'D 主板 2 板-甲', trade_date: today, prev_close: 12.0, last_close: 13.16, high: 13.16, low: 12.8, volume: 10000, amount: 13160000, industry: '汽车', theme_tags: ['ev_chain'], limit_time: '09:35:00', is_exploded: false, is_limit_down: false, prev_pct: 0.097 },
  { symbol: '600011', name: 'E 主板 2 板-乙', trade_date: today, prev_close: 15.0, last_close: 16.45, high: 16.45, low: 15.9, volume: 7000, amount: 11515000, industry: '化工', theme_tags: ['ev_chain'], limit_time: '10:10:00', is_exploded: false, is_limit_down: false, prev_pct: 0.098 },
  { symbol: '600020', name: 'F 主板 3 板-甲', trade_date: today, prev_close: 18.0, last_close: 19.74, high: 19.74, low: 19.2, volume: 6000, amount: 11844000, industry: '医药', theme_tags: ['innovative_drug'], limit_time: '09:25:00', is_exploded: false, is_limit_down: false, prev_pct: 0.099 },
  { symbol: '600030', name: 'G 主板 7 板', trade_date: today, prev_close: 30.0, last_close: 32.9, high: 32.9, low: 31.5, volume: 9000, amount: 29610000, industry: '传媒', theme_tags: ['ai_chain'], limit_time: '09:30:00', is_exploded: false, is_limit_down: false, prev_pct: 0.10 },
  { symbol: '600031', name: 'H 主板 5 板', trade_date: today, prev_close: 25.0, last_close: 27.4, high: 27.4, low: 26.5, volume: 7000, amount: 19180000, industry: '传媒', theme_tags: ['ai_chain'], limit_time: '09:40:00', is_exploded: false, is_limit_down: false, prev_pct: 0.099 },
  { symbol: '600032', name: 'I 主板 5 板', trade_date: today, prev_close: 18.0, last_close: 19.73, high: 19.73, low: 19.1, volume: 5000, amount: 9865000, industry: '传媒', theme_tags: ['ai_chain'], limit_time: '10:20:00', is_exploded: false, is_limit_down: false, prev_pct: 0.099 },
  { symbol: '600033', name: 'J 主板 4 板', trade_date: today, prev_close: 12.0, last_close: 13.15, high: 13.15, low: 12.8, volume: 6000, amount: 7890000, industry: '汽车', theme_tags: ['ev_chain'], limit_time: '11:00:00', is_exploded: false, is_limit_down: false, prev_pct: 0.099 },
  { symbol: '600040', name: 'K 主板炸板回封', trade_date: today, prev_close: 10.0, last_close: 10.95, high: 11.0, low: 10.5, volume: 20000, amount: 21900000, industry: '地产', theme_tags: ['ai_chain'], limit_time: '14:30:00', is_exploded: true, is_limit_down: false, is_re_seal: true, prev_pct: 0.03 },
  { symbol: '600041', name: 'L 主板炸板回封', trade_date: today, prev_close: 15.0, last_close: 16.45, high: 16.5, low: 15.8, volume: 15000, amount: 24675000, industry: '化工', theme_tags: ['ev_chain'], limit_time: '14:00:00', is_exploded: true, is_limit_down: false, is_re_seal: true, prev_pct: 0.04 },
  { symbol: '600042', name: 'M 主板炸板回封', trade_date: today, prev_close: 8.0, last_close: 8.78, high: 8.8, low: 8.5, volume: 12000, amount: 10536000, industry: '医药', theme_tags: ['innovative_drug'], limit_time: '13:30:00', is_exploded: true, is_limit_down: false, is_re_seal: true, prev_pct: 0.02 },
  { symbol: '600099', name: 'N 主板跌停', trade_date: today, prev_close: 10.0, last_close: 9.0, high: 9.1, low: 9.0, volume: 20000, amount: 18000000, industry: '地产', theme_tags: [], limit_time: null, is_exploded: false, is_limit_down: true, prev_pct: -0.04 },
  { symbol: '300001', name: 'O 创业板 20%', trade_date: today, prev_close: 10.0, last_close: 12.0, high: 12.0, low: 11.5, volume: 9000, amount: 10800000, industry: '新能源', theme_tags: ['solar'], limit_time: '11:00:00', is_exploded: false, is_limit_down: false, prev_pct: 0.05 },
]

export const MOCK_PREV_POOL = [
  { symbol: '600010', board_count: 1 },
  { symbol: '600011', board_count: 1 },
  { symbol: '600020', board_count: 2 },
  { symbol: '600030', board_count: 6 },
  { symbol: '600031', board_count: 4 },
  { symbol: '600032', board_count: 4 },
  { symbol: '600033', board_count: 3 },
  { symbol: '600001', board_count: 1 },
  { symbol: '600002', board_count: 1 },
  { symbol: '600003', board_count: 1 },
]

export const MOCK_LADDER: MockLadder[] = [
  { board: 5, count: 3, symbols: ['600030', '600031', '600032'], first_limit_time: '09:30:00', last_limit_time: '10:20:00' },
  { board: 3, count: 2, symbols: ['600020', '600033'], first_limit_time: '09:25:00', last_limit_time: '11:00:00' },
  { board: 2, count: 2, symbols: ['600010', '600011'], first_limit_time: '09:35:00', last_limit_time: '10:10:00' },
  { board: 1, count: 5, symbols: ['600001', '600002', '600003', '300001', '600040'], first_limit_time: '10:30:00', last_limit_time: '14:30:00' },
]

export const MOCK_LIMIT_UP_ITEMS: MockLimitUpItem[] = [
  { symbol: '600030', name: 'G 主板 7 板', trade_date: today, limit_time: '09:30:00', first_limit_price: 32.9, last_close: 32.9, turnover: 0, amount: 29610000, industry: '传媒', theme_tags: ['ai_chain'], board_count: 7, prev_pct: 0.10 },
  { symbol: '600031', name: 'H 主板 5 板', trade_date: today, limit_time: '09:40:00', first_limit_price: 27.4, last_close: 27.4, turnover: 0, amount: 19180000, industry: '传媒', theme_tags: ['ai_chain'], board_count: 5, prev_pct: 0.099 },
  { symbol: '600032', name: 'I 主板 5 板', trade_date: today, limit_time: '10:20:00', first_limit_price: 19.73, last_close: 19.73, turnover: 0, amount: 9865000, industry: '传媒', theme_tags: ['ai_chain'], board_count: 5, prev_pct: 0.099 },
  { symbol: '600020', name: 'F 主板 3 板-甲', trade_date: today, limit_time: '09:25:00', first_limit_price: 19.74, last_close: 19.74, turnover: 0, amount: 11844000, industry: '医药', theme_tags: ['innovative_drug'], board_count: 3, prev_pct: 0.099 },
  { symbol: '600033', name: 'J 主板 4 板', trade_date: today, limit_time: '11:00:00', first_limit_price: 13.15, last_close: 13.15, turnover:  0, amount: 7890000, industry: '汽车', theme_tags: ['ev_chain'], board_count: 4, prev_pct: 0.099 },
  { symbol: '600010', name: 'D 主板 2 板-甲', trade_date: today, limit_time: '09:35:00', first_limit_price: 13.16, last_close: 13.16, turnover: 0, amount: 13160000, industry: '汽车', theme_tags: ['ev_chain'], board_count: 2, prev_pct: 0.097 },
  { symbol: '600011', name: 'E 主板 2 板-乙', trade_date: today, limit_time: '10:10:00', first_limit_price: 16.45, last_close: 16.45, turnover: 0, amount: 11515000, industry: '化工', theme_tags: ['ev_chain'], board_count: 2, prev_pct: 0.098 },
  { symbol: '600001', name: 'A 主板 1 板-甲', trade_date: today, limit_time: '10:30:00', first_limit_price: 10.97, last_close: 10.97, turnover: 0, amount: 13164000, industry: '电子', theme_tags: ['ai_chain', 'semiconductor'], board_count: 1, prev_pct: 0.01 },
  { symbol: '600002', name: 'B 主板 1 板-乙', trade_date: today, limit_time: '13:05:00', first_limit_price: 21.93, last_close: 21.93, turnover: 0, amount: 10965000, industry: '通信', theme_tags: ['ai_chain'], board_count: 1, prev_pct: 0.02 },
  { symbol: '600003', name: 'C 主板 1 板-丙', trade_date: today, limit_time: '14:00:00', first_limit_price: 8.78, last_close: 8.78, turnover: 0, amount: 7024000, industry: '医药', theme_tags: ['innovative_drug'], board_count: 1, prev_pct: -0.005 },
  { symbol: '600040', name: 'K 主板炸板回封', trade_date: today, limit_time: '14:30:00', first_limit_price: 11.0, last_close: 10.95, turnover: 0, amount: 21900000, industry: '地产', theme_tags: ['ai_chain'], board_count: 1, is_re_seal: true, prev_pct: 0.03 },
  { symbol: '300001', name: 'O 创业板 20%', trade_date: today, limit_time: '11:00:00', first_limit_price: 12.0, last_close: 12.0, turnover: 0, amount: 10800000, industry: '新能源', theme_tags: ['solar'], board_count: 1, prev_pct: 0.05 },
]

export const MOCK_STATS = {
  limit_up_total: 12,
  limit_down_total: 1,
  exploded_total: 3,
  re_seal_count: 3,
  max_board: 7,
  seal_rate: 1.0,
  exploded_rate: 3 / (12 + 3),
  ladder_count: 4,
  promotion_rate: { by_board: { '1': 0.67, '2': 1.0, '3': 1.0, '4': 0.5, '6': 1.0 }, overall: 0.7 },
  // 新增 6 张深度
  final_explode_rate: 0.163,
  prev_limit_up_feedback: -0.0213,
  prev_consecutive_feedback: -0.0249,
  promotion_by_board_overall: 0.267,
  main_line_concentration: 0.42,
  high_risk_score: 45,
}

// ============================================================
// P2 v2.0 新增 Mock
// ============================================================

/** 14 日情绪时序 (含 4 维指标 + 盘后总结) */
export const MOCK_EMOTION_14D: MockEmotionBar[] = [
  { date: '2026-10-01', score: 75, phase: '活跃', high_pos_avg: 0.05, decline_coverage: 0.20, high_collapse: 0, high_promotion: 0.6, summary: '高位活跃, 连板稳定' },
  { date: '2026-10-02', score: 70, phase: '活跃', high_pos_avg: 0.04, decline_coverage: 0.25, high_collapse: 0, high_promotion: 0.5, summary: '晋级率 50%, 持续性一般' },
  { date: '2026-10-03', score: 50, phase: '分歧', high_pos_avg: 0.02, decline_coverage: 0.30, high_collapse: 0, high_promotion: 0.4, summary: '高位分歧, 资金观望' },
  { date: '2026-10-04', score: 60, phase: '活跃', high_pos_avg: 0.04, decline_coverage: 0.20, high_collapse: 0, high_promotion: 0.5, summary: '情绪修复, 广度回升' },
  { date: '2026-10-05', score: 35, phase: '退潮', high_pos_avg: -0.01, decline_coverage: 0.50, high_collapse: 1, high_promotion: 0.3, summary: '炸板率上升, 高度坍缩' },
  { date: '2026-10-06', score: 70, phase: '活跃', high_pos_avg: 0.05, decline_coverage: 0.20, high_collapse: 0, high_promotion: 0.5, summary: '主线修复, 概念聚焦' },
  { date: '2026-10-07', score: 70, phase: '活跃', high_pos_avg: 0.06, decline_coverage: 0.15, high_collapse: 0, high_promotion: 0.6, summary: '持续性走强' },
  { date: '2026-10-08', score: 70, phase: '活跃', high_pos_avg: 0.04, decline_coverage: 0.20, high_collapse: 0, high_promotion: 0.5, summary: '广度稳定' },
  { date: '2026-10-09', score: 75, phase: '活跃', high_pos_avg: 0.06, decline_coverage: 0.15, high_collapse: 0, high_promotion: 0.6, summary: '高位活跃' },
  { date: '2026-10-10', score: 70, phase: '活跃', high_pos_avg: 0.05, decline_coverage: 0.20, high_collapse: 0, high_promotion: 0.5, summary: '主线稳定' },
  { date: '2026-10-13', score: 60, phase: '活跃', high_pos_avg: 0.03, decline_coverage: 0.30, high_collapse: 0, high_promotion: 0.4, summary: '广度回落' },
  { date: '2026-10-14', score: 50, phase: '分歧', high_pos_avg: 0.01, decline_coverage: 0.40, high_collapse: 0, high_promotion: 0.3, summary: '高位分歧' },
  { date: '2026-10-15', score: 40, phase: '退潮', high_pos_avg: -0.02, decline_coverage: 0.50, high_collapse: 0, high_promotion: 0.3, summary: '炸板率上升' },
  { date: '2026-10-16', score: 36, phase: '分歧', high_pos_avg: -0.007, decline_coverage: 0.50, high_collapse: 0, high_promotion: 0.33, summary: '近交易日收盘后快照, 高位 5板 / 3板 / 2板 局部高度。低位活跃, 资金聚焦主线。' },
]

/** 4 段晋级率 */
export const MOCK_PROMOTION_SEGS: MockPromotionSeg[] = [
  { from_board: 1, to_label: '1进2', promoted: 4, total: 30, ratio: 0.13 },
  { from_board: 2, to_label: '2进3', promoted: 1, total: 2, ratio: 0.50 },
  { from_board: 3, to_label: '3进4', promoted: 1, total: 2, ratio: 0.50 },
  { from_board: 5, to_label: '5进6', promoted: 0, total: 2, ratio: 0 },
]

/** 概念热度榜 (5 维加权) */
export const MOCK_THEME_HEAT: MockThemeHeat[] = [
  { theme_id: 'ai_chain', theme_name: 'AI 算力链', max_board: 7, limit_up_count: 5, consecutive_count: 3, heat_score: 100 },
  { theme_id: 'ev_chain', theme_name: '新能源汽车链', max_board: 4, limit_up_count: 4, consecutive_count: 2, heat_score: 66 },
  { theme_id: 'innovative_drug', theme_name: '创新药', max_board: 3, limit_up_count: 2, consecutive_count: 1, heat_score: 45 },
  { theme_id: 'solar', theme_name: '光伏', max_board: 1, limit_up_count: 1, consecutive_count: 0, heat_score: 22 },
  { theme_id: 'semiconductor', theme_name: '半导体', max_board: 1, limit_up_count: 1, consecutive_count: 0, heat_score: 18 },
]

// ============================================================
// 趋势题材 (P1.2 已用, 保留)
// ============================================================
function genEWIndex(seed: number, n = 10): MockThemeBar[] {
  const base = new Date('2026-10-01')
  const bars: MockThemeBar[] = []
  let value = 1000
  for (let i = 0; i < n; i++) {
    const d = new Date(base)
    d.setDate(base.getDate() + i)
    const r = ((Math.sin(i + seed) + 1) / 2) * 0.06 - 0.01
    const prev = value
    value = value * (1 + r)
    bars.push({
      date: d.toISOString().slice(0, 10),
      open: Number(prev.toFixed(4)),
      high: Number(Math.max(prev, value).toFixed(4)),
      low: Number(Math.min(prev, value).toFixed(4)),
      close: Number(value.toFixed(4)),
      volume: 2000 + i * 100,
      amount: 100_000_000 + i * 5_000_000,
    })
  }
  return bars
}

export const MOCK_THEMES = [
  { theme_id: 'ai_chain', theme_name: 'AI 算力链', industries: ['电子', '通信'], bars: genEWIndex(1), change_pct: 0.058, phase: '启动' as const },
  { theme_id: 'ev_chain', theme_name: '新能源汽车链', industries: ['汽车', '化工'], bars: genEWIndex(2), change_pct: 0.032, phase: '修复' as const },
  { theme_id: 'innovative_drug', theme_name: '创新药', industries: ['医药'], bars: genEWIndex(3), change_pct: -0.012, phase: '震荡' as const },
  { theme_id: 'solar', theme_name: '光伏', industries: ['新能源'], bars: genEWIndex(4), change_pct: 0.045, phase: '高潮' as const },
  { theme_id: 'semiconductor', theme_name: '半导体', industries: ['电子'], bars: genEWIndex(5), change_pct: -0.034, phase: '冰点' as const },
]

export const MOCK_ATTRIBUTIONS: Record<string, MockAttribution[]> = {
  ai_chain: [
    { node: '上游-光模块', weight: 1.5, avg_change_pct: 0.075, contribution: 0.1125, stocks: [{ symbol: '300750', change_pct: 0.10 }, { symbol: '002594', change_pct: 0.05 }] },
    { node: '中游-服务器', weight: 1.2, avg_change_pct: 0.03, contribution: 0.036, stocks: [{ symbol: '000858', change_pct: 0.03 }] },
    { node: '下游-应用', weight: 0.8, avg_change_pct: 0.015, contribution: 0.012, stocks: [{ symbol: '601318', change_pct: 0.02 }, { symbol: '600519', change_pct: 0.01 }] },
  ],
  solar: [
    { node: '上游-硅料', weight: 1.4, avg_change_pct: 0.06, contribution: 0.084, stocks: [{ symbol: '601012', change_pct: 0.07 }, { symbol: '002709', change_pct: 0.05 }] },
    { node: '中游-组件', weight: 1.1, avg_change_pct: 0.04, contribution: 0.044, stocks: [{ symbol: '002459', change_pct: 0.04 }, { symbol: '688223', change_pct: 0.04 }] },
    { node: '下游-电站', weight: 0.9, avg_change_pct: 0.02, contribution: 0.018, stocks: [{ symbol: '601877', change_pct: 0.02 }] },
  ],
}