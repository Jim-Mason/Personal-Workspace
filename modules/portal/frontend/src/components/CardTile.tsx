/**
 * 功能卡片：对应设计稿网格里的单个磁贴。
 *
 * 结构说明：外层 `.tile` 只负责外观（底色、圆角、阴影、拖拽），
 * 内层 `.tile-open` 是真正可聚焦、可点击的**链接本体**，
 * 右上角的编辑/删除按钮与它是兄弟节点 —— 避免把按钮套进按钮这种无效结构。
 *
 * 为什么是 `<a>` 而不是 `<button>` + `window.open`：
 * 链接导航不受浏览器弹窗拦截策略约束；而 `window.open(url,'_blank','noopener')`
 * 按规范会**固定返回 null**，无法用来判断是否被拦截（详见 lib/launcher.ts 里的历史坑）。
 * 换成 `<a>` 后，中键 / Ctrl+点击 / 右键「在新标签页打开」也都能正常用了。
 *
 * 环境地址：`card.endpoints` 非空时这张卡片挂了多个环境，点击会被父组件拦下来
 * 先弹环境列表（`onOpen` 里 `preventDefault`），因此那种卡片上中键/Ctrl+点击
 * 不再直达 —— 这是"先确认环境"的已知代价，细节见 EndpointPicker。
 */

import type { DragEvent, MouseEvent as ReactMouseEvent } from 'react'
import type { Card } from '../lib/types'
import { buildLaunchTarget } from '../lib/launcher'
import { CardIcon } from './CardIcon'
import { EditIcon, ExternalIcon, GripIcon, TrashIcon } from './Icons'

interface CardTileProps {
  card: Card
  onOpen: (event: ReactMouseEvent<HTMLAnchorElement>, card: Card) => void
  /** 管理员可见：悬停显示编辑 / 删除 */
  admin?: boolean
  onEdit?: (card: Card) => void
  onDelete?: (card: Card) => void
  /** 排序模式下：整卡可拖拽，点击不跳转 */
  reorderable?: boolean
  dragging?: boolean
  dropBefore?: boolean
  onDragStart?: (event: DragEvent<HTMLDivElement>, card: Card) => void
  onDragOver?: (event: DragEvent<HTMLDivElement>, card: Card) => void
  onDrop?: (event: DragEvent<HTMLDivElement>, card: Card) => void
  onDragEnd?: (event: DragEvent<HTMLDivElement>) => void
}

export function CardTile({
  card,
  onOpen,
  admin = false,
  onEdit,
  onDelete,
  reorderable = false,
  dragging = false,
  dropBefore = false,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: CardTileProps) {
  const isProxy = card.open_mode === 'proxy'
  const { url, newTab } = buildLaunchTarget(card)
  // 挂了环境地址的卡片：点击由父组件接管，先弹环境列表
  const hasEndpoints = card.endpoints.length > 0
  const endpointCount = card.endpoints.length

  const classNames = [
    'tile',
    card.enabled ? '' : 'is-disabled',
    admin && !reorderable ? 'has-actions' : '',
    reorderable ? 'is-reorderable' : '',
    hasEndpoints ? 'has-endpoints' : '',
    dragging ? 'is-dragging' : '',
    dropBefore ? 'is-drop-before' : '',
  ]
    .filter(Boolean)
    .join(' ')

  const openHint = hasEndpoints
    ? `共 ${endpointCount} 个环境地址，点击选择要打开哪一个`
    : isProxy
      ? `经跳板机代理：${card.target_url}`
      : `浏览器直连：${card.target_url}`

  return (
    <div
      className={classNames}
      data-bg={card.bg_style || undefined}
      draggable={reorderable}
      onDragStart={reorderable ? (event) => onDragStart?.(event, card) : undefined}
      onDragOver={reorderable ? (event) => onDragOver?.(event, card) : undefined}
      onDrop={reorderable ? (event) => onDrop?.(event, card) : undefined}
      onDragEnd={reorderable ? onDragEnd : undefined}
    >
      {/*
        target="_blank" 时带上 rel="noopener noreferrer"：不让被打开的页面
        通过 window.opener 反向操纵门户（注意这与 window.open 的返回值无关）。
        Enter 键由链接原生行为处理，不需要额外的 onKeyDown。

        href 仍然保留（挂了环境地址时取第一条的入口）——这样在 Cmd/中键这类
        浏览器原生行为下至少不会变成空链接，也能被"复制链接地址"用上。
      */}
      <a
        className="tile-open"
        href={hasEndpoints ? card.endpoints[0].launch_url || card.endpoints[0].url : url}
        target={newTab ? '_blank' : undefined}
        rel={newTab ? 'noopener noreferrer' : undefined}
        onClick={(event) => onOpen(event, card)}
        title={openHint}
      >
        <CardIcon card={card} size={46} />

        <span className="tile-body">
          <span className="tile-title">{card.title}</span>
          {card.description ? <span className="tile-desc">{card.description}</span> : null}
        </span>

        <span className="tile-foot">
          {hasEndpoints ? (
            <span className="badge badge-env">{endpointCount} 个环境</span>
          ) : (
            <span className={`badge ${isProxy ? '' : 'badge-direct'}`}>
              {isProxy ? '代理访问' : '直连访问'}
            </span>
          )}
          {card.click_count > 0 ? (
            <span className="badge badge-muted">打开 {card.click_count} 次</span>
          ) : null}
          {!card.enabled ? <span className="badge badge-off">已停用</span> : null}
          <span className="tile-external">
            <ExternalIcon size={14} />
          </span>
        </span>
      </a>

      {admin && !reorderable ? (
        <div className="tile-actions">
          <button
            type="button"
            className="tile-action"
            onClick={(event) => {
              event.stopPropagation()
              onEdit?.(card)
            }}
            title={`编辑「${card.title}」`}
            aria-label={`编辑 ${card.title}`}
          >
            <EditIcon size={14} />
          </button>
          <button
            type="button"
            className="tile-action is-danger"
            onClick={(event) => {
              event.stopPropagation()
              onDelete?.(card)
            }}
            title={`删除「${card.title}」`}
            aria-label={`删除 ${card.title}`}
          >
            <TrashIcon size={14} />
          </button>
        </div>
      ) : null}

      {reorderable ? (
        <span className="tile-grip" aria-hidden="true">
          <GripIcon size={16} />
        </span>
      ) : null}
    </div>
  )
}
