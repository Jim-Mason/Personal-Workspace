/** 卡片外观相关的常量与工具。 */

import type { BgStyle } from './types'

/** 底色预设（与后端 BG_STYLES 保持一致，空串 = 默认蓝色渐变） */
export const BG_STYLE_OPTIONS: Array<{ value: BgStyle; label: string }> = [
  { value: '', label: '默认' },
  { value: 'default', label: '云雾蓝' },
  { value: 'blue', label: '天青' },
  { value: 'violet', label: '紫罗兰' },
  { value: 'mint', label: '薄荷' },
  { value: 'sand', label: '暖沙' },
  { value: 'rose', label: '浅玫' },
  { value: 'slate', label: '石板' },
]

/** 文字图标配色（对应 icon_style 字段） */
export const ICON_STYLES = [
  'blue',
  'indigo',
  'violet',
  'cyan',
  'teal',
  'green',
  'amber',
  'rose',
] as const

export const ICON_STYLE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'blue', label: '天蓝' },
  { value: 'indigo', label: '靛蓝' },
  { value: 'violet', label: '紫罗兰' },
  { value: 'cyan', label: '青色' },
  { value: 'teal', label: '蓝绿' },
  { value: 'green', label: '绿色' },
  { value: 'amber', label: '琥珀' },
  { value: 'rose', label: '玫红' },
]

/** 主色快捷预设 */
export const ACCENT_PRESETS = [
  '#2b6cf6',
  '#4f57d6',
  '#7a4fd0',
  '#1f7fa8',
  '#17826f',
  '#1f8a4d',
  '#b0791c',
  '#c44257',
]

const HEX_RE = /^#[0-9a-f]{6}$/i

/** 主色是否合法（后端只接受 #RRGGBB） */
export function isValidAccent(value: string): boolean {
  return HEX_RE.test((value || '').trim())
}

/** 图标是不是"上传的图片" */
export function isImageIcon(iconUrl: string | undefined | null): boolean {
  return Boolean(iconUrl && iconUrl.trim())
}

/**
 * 主色换算成图标底色 / 前景色。
 * 用极浅的同色相做底、原色做字，保证在任何卡片底色上都够清晰。
 */
export function accentTint(accent: string): { bg: string; color: string } | null {
  if (!isValidAccent(accent)) return null
  const hex = accent.trim().toLowerCase()
  const r = parseInt(hex.slice(1, 3), 16)
  const g = parseInt(hex.slice(3, 5), 16)
  const b = parseInt(hex.slice(5, 7), 16)
  const lighten = (channel: number) => Math.round(channel + (255 - channel) * 0.84)
  return {
    bg: `rgb(${lighten(r)}, ${lighten(g)}, ${lighten(b)})`,
    color: hex,
  }
}

/** 把 external / relative 图标地址转成可安全放进 img src 的形式 */
export function safeIconSrc(iconUrl: string): string {
  const url = (iconUrl || '').trim()
  if (!url) return ''
  if (url.startsWith('/uploads/')) return url
  if (/^https?:\/\//i.test(url)) return url
  return ''
}
