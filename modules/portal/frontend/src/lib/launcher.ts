/**
 * 卡片打开逻辑：算出「打开哪个地址、怎么打开」，以及上报一次点击。
 *
 * ⚠️ 这里刻意**不提供** `window.open` 式的打开函数，原因见下方 openCard 的历史说明。
 * 卡片磁贴与后台「预览」都改用真实的 `<a href target="_blank">`：
 * 链接导航属于用户主动导航，**不受浏览器弹窗拦截策略约束**，
 * 顺带还能用中键 / Ctrl+点击 / 右键「在新标签页打开」。
 */

import type { Card, CardEndpoint } from './types'

const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

/** 计算卡片最终的打开地址与打开方式。 */
export interface LaunchTarget {
  /** 直连模式是目标地址本身；代理模式是网关入口（由后端在 launch_url 里算好） */
  url: string
  /** true = 新标签页打开；false = 当前标签页跳转 */
  newTab: boolean
}

/**
 * 纯函数，便于单测：不碰 window，只做取值与兜底。
 * `launch_url` 为空时回退到 `target_url`（老数据 / 导入的配置可能没有它）。
 */
export function buildLaunchTarget(
  card: Pick<Card, 'launch_url' | 'target_url' | 'open_in_new_tab'>,
): LaunchTarget {
  return {
    url: card.launch_url || card.target_url,
    newTab: Boolean(card.open_in_new_tab),
  }
}

/**
 * 选中的某条环境地址怎么打开。
 *
 * 与 `buildLaunchTarget` 同一个规则，只是数据源换成了环境地址 —— 后端已经按
 * 这条地址**自己的** open_mode 把 `launch_url` 算好了（direct = 原地址，
 * proxy = `/gw/{环境标识}/`），前端不需要再判断一次。
 */
export function buildEndpointLaunchTarget(
  endpoint: Pick<CardEndpoint, 'launch_url' | 'url'>,
  card: Pick<Card, 'open_in_new_tab'>,
): LaunchTarget {
  return {
    url: endpoint.launch_url || endpoint.url,
    newTab: Boolean(card.open_in_new_tab),
  }
}

/**
 * 上报一次打开。使用 keepalive，保证在新标签页/当前页跳走后请求仍能送达。
 * 失败时静默忽略——统计不该阻塞用户操作。
 *
 * `endpointSlug` 是用户点开的那条环境地址（点卡片本体时省略）。点击量仍然记在
 * 卡片上，这个参数只用来在审计明细里留下"点的是哪个环境"。
 */
export function reportClick(card: Card, endpointSlug?: string): void {
  try {
    const suffix = endpointSlug ? `?endpoint=${encodeURIComponent(endpointSlug)}` : ''
    void fetch(`${BASE}/api/cards/${card.id}/click${suffix}`, {
      method: 'POST',
      credentials: 'include',
      keepalive: true,
      headers: { Accept: 'application/json' },
    }).catch(() => undefined)
  } catch {
    // 忽略
  }
}

/*
 * 历史坑（务必不要回退）：
 *
 * 早先这里用 `window.open(url, '_blank', 'noopener,noreferrer')`，并以
 * `Boolean(win)` 作为「是否被浏览器拦截」的判据，被拦截时给用户弹提示。
 *
 * 但按 HTML 规范，**只要 windowFeatures 里出现 `noopener`，window.open() 就固定返回 `null`**，
 * 哪怕新标签页已经正常打开（`noreferrer` 同样会隐含 noopener）。
 * 于是返回值恒为 null → 每次都误报「浏览器拦截了新标签页」，
 * 而用户其实每次都已经成功打开了页面，只会被这条假提示反复困扰。
 *
 * 结论：**永远不要用 window.open 的返回值判断是否被拦截**，
 * 更不要为此在 windowFeatures 里塞 noopener。
 * 需要"新标签页打开"就用真实 `<a href target="_blank" rel="noopener noreferrer">`。
 */
