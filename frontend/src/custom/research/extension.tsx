/**
 * IRP 前端扩展 — P1.3。
 *
 * 3 个路由:
 *   /research                  ResearchLauncher (tabs 切换 3 模式)
 *   /research/short-term       ShortTermWorkspace (独立完整页, 含 header)
 *   /research/theme-radar      ThemeRadarWorkspace (独立完整页, 含 header)
 *
 * Launcher 内部用 tabs 切换 short-term / theme-radar / assistant 模式;
 * 子路由保留给书签分享。
 *
 * 按 TSP 通道 2 标准实现 (docs/secondary-development.md §3)。
 * 删除本目录 + pnpm build = 完全卸载。
 *
 * 仿 assistant 模式: 插槽注册表把 component 视作 AnySlotContext,
 * 内部按需 cast 到具体 slot 类型。
 */
import { Sparkles } from 'lucide-react'
import type {
  FrontendExtension,
  FrontendSlotContextMap,
} from '@/extensions/types'
import ResearchLauncher from './pages/ResearchLauncher'
import ShortTermWorkspace from './pages/ShortTermWorkspace'
import ThemeRadarWorkspace from './pages/ThemeRadarWorkspace'

type NavigationContext = FrontendSlotContextMap['layout.navigation.extra']
type AnySlotContext = FrontendSlotContextMap[keyof FrontendSlotContextMap]

function ResearchNavigationExtra(props: AnySlotContext) {
  const { collapsed } = props as NavigationContext
  if (collapsed) return null
  return (
    <div className="px-3 py-1 text-xs text-muted">IRP · P1.3</div>
  )
}

const extension: FrontendExtension = {
  id: 'research.studio',
  apiVersion: 1,
  routes: [
    {
      id: 'research-home',
      path: '/research',
      component: ResearchLauncher,
    },
    {
      id: 'research-short-term',
      path: '/research/short-term',
      component: ShortTermWorkspace,
    },
    {
      id: 'research-theme-radar',
      path: '/research/theme-radar',
      component: ThemeRadarWorkspace,
    },
  ],
  navigation: [
    {
      id: 'research-home',
      routeId: 'research-home',
      label: '研究工作台',
      icon: Sparkles,
      order: 500,
    },
  ],
  slots: [
    {
      name: 'layout.navigation.extra',
      id: 'research-entry',
      order: 100,
      component: ResearchNavigationExtra,
    },
  ],
}

export default extension