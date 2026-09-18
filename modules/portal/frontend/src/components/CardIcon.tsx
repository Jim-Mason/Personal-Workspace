/** 卡片图标：图片图标 / 字符图标 + 主色，三处（磁贴、管理表格、表单预览）共用。 */

import type { CSSProperties } from 'react'
import { accentTint, safeIconSrc } from '../lib/appearance'
import type { Card } from '../lib/types'

type CardIconSource = Pick<Card, 'icon' | 'icon_style' | 'icon_url' | 'accent_color' | 'title'>

interface CardIconProps {
  card: CardIconSource
  size?: number
  /** 追加类名，例如 'icon-preview' */
  className?: string
  /** 是否懒加载图片（表格里数量多时有用） */
  lazy?: boolean
}

export function CardIcon({ card, size = 46, className, lazy = true }: CardIconProps) {
  const src = safeIconSrc(card.icon_url)
  const tint = accentTint(card.accent_color)

  const style: CSSProperties = {
    width: size,
    height: size,
    fontSize: Math.max(11, Math.round(size * 0.42)),
    borderRadius: Math.round(size * 0.27),
  }

  if (tint) {
    if (src) {
      // 图片图标：主色改作描边，底色保持白，避免彩色底吃掉图片细节
      style.background = 'rgba(255, 255, 255, 0.88)'
      style.border = `1px solid ${tint.color}`
    } else {
      style.background = tint.bg
      style.color = tint.color
    }
  }

  const classes = ['tile-icon', src ? 'is-image' : '', className ?? ''].filter(Boolean).join(' ')

  return (
    <span className={classes} data-style={card.icon_style || 'blue'} style={style}>
      {src ? (
        <img src={src} alt="" loading={lazy ? 'lazy' : undefined} draggable={false} />
      ) : (
        card.icon || card.title.slice(0, 1)
      )}
    </span>
  )
}
