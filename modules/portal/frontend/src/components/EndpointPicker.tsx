/**
 * 环境选择弹窗：一张卡片挂了多条地址时，点卡片先弹这个列表，选中一条再跳转。
 *
 * 为什么"先弹列表"而不是"点卡片直接跳默认环境"：
 * 同一套系统的生产 / 测试经常长得一模一样，误点生产去改数据代价很高；
 * 多一次点击换来一次明确的确认，是目前对用户更划算的取舍。
 *
 * ⚠️ 代价要说清楚：因为必须拦截点击，`<a>` 的中键 / Ctrl+点击 / 右键「在新标签页
 * 打开」在这里就**不生效了**（浏览器原生行为拿不到"该打开哪一条"这个信息）。
 * 打开动作全部落在每行的「打开」按钮上，那仍然是真实 `<a href>`，弹出拦截策略拦不住。
 */

import { buildEndpointLaunchTarget } from '../lib/launcher'
import type { Card, CardEndpoint } from '../lib/types'
import { ExternalIcon } from './Icons'
import { Modal } from './Modal'

interface EndpointPickerProps {
  card: Card
  /** 用户点了某一条的「打开」 */
  onOpen: (card: Card, endpoint: CardEndpoint) => void
  onClose: () => void
}

export function EndpointPicker({ card, onOpen, onClose }: EndpointPickerProps) {
  return (
    <Modal
      title={card.title}
      subtitle={`这张卡片有 ${card.endpoints.length} 个环境地址，选一个打开。`}
      onClose={onClose}
      width={660}
    >
      <div className="endpoint-picker">
        {card.endpoints.map((endpoint) => {
          const { url, newTab } = buildEndpointLaunchTarget(endpoint, card)
          const isProxy = endpoint.open_mode === 'proxy'
          return (
            <div className="endpoint-pick-row" key={endpoint.id}>
              <div className="endpoint-pick-main">
                <span className="endpoint-pick-name" title={endpoint.name}>
                  {endpoint.name}
                </span>
                <span className="endpoint-pick-url mono" title={endpoint.url}>
                  {endpoint.url}
                </span>
              </div>
              <span
                className={`badge ${isProxy ? '' : 'badge-direct'}`}
                title={isProxy ? '经跳板机代理，本地无需可达' : '浏览器直连，需要本地本身能访问到'}
              >
                {isProxy ? '代理' : '直连'}
              </span>
              <a
                className="btn btn-sm btn-primary"
                href={url}
                target={newTab ? '_blank' : undefined}
                rel={newTab ? 'noopener noreferrer' : undefined}
                onClick={() => onOpen(card, endpoint)}
                title={isProxy ? `经跳板机打开：${endpoint.url}` : `浏览器直连：${endpoint.url}`}
              >
                <ExternalIcon size={13} />
                打开
              </a>
            </div>
          )
        })}
      </div>

      <p className="endpoint-picker-note">
        提示：需要新标签页打开可以勾选表单里的「在新标签页打开」。
        这一层列表会拦掉中键与右键，是"先确认环境"的代价。
      </p>
    </Modal>
  )
}
