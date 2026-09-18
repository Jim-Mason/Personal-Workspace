/** 工具门户首页：卡片网格 + 搜索 + 分组筛选 + 管理员拖拽排序 / 卡片上直接改。 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
// 别名导入：本文件里 `MouseEvent` 还指 DOM 原生类型（见下方 mousedown 监听），
// 直接 import { MouseEvent } from 'react' 会把它遮蔽掉。
import type { DragEvent, MouseEvent as ReactMouseEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { CardFormModal } from '../components/CardFormModal'
import { CardTile } from '../components/CardTile'
import { EndpointPicker } from '../components/EndpointPicker'
import { LogoutIcon, SearchIcon, SettingsIcon, SortIcon } from '../components/Icons'
import { ConfirmDialog } from '../components/Modal'
import { useToast } from '../components/Toast'
import { ApiError, api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { reportClick } from '../lib/launcher'
import type { Card, CardEndpoint, CardGroup } from '../lib/types'

/** 相邻卡片之间的排序步长，留出插空余地 */
const SORT_STEP = 10

export function ToolboxPage() {
  const { user, logout } = useAuth()
  const toast = useToast()
  const navigate = useNavigate()
  const isAdmin = user?.role === 'admin'

  const [cards, setCards] = useState<Card[]>([])
  const [groups, setGroups] = useState<CardGroup[]>([])
  const [keyword, setKeyword] = useState('')
  const [activeGroup, setActiveGroup] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement | null>(null)

  // 卡片上直接改
  const [editing, setEditing] = useState<Card | null>(null)
  const [pendingDelete, setPendingDelete] = useState<Card | null>(null)
  const [deleting, setDeleting] = useState(false)
  /** 挂了多个环境地址的卡片：先弹环境列表，选中一条再跳 */
  const [picking, setPicking] = useState<Card | null>(null)

  // 拖拽排序
  const [reorderMode, setReorderMode] = useState(false)
  const [dragId, setDragId] = useState<number | null>(null)
  const [overId, setOverId] = useState<number | null>(null)
  const [savingOrder, setSavingOrder] = useState(false)
  const [orderSaved, setOrderSaved] = useState(false)
  const savedTimer = useRef<number | null>(null)

  const reload = useCallback(async () => {
    const [cardList, groupList] = await Promise.all([api.listCards(), api.listGroups()])
    setCards(cardList)
    setGroups(groupList)
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const [cardList, groupList] = await Promise.all([api.listCards(), api.listGroups()])
        if (cancelled) return
        setCards(cardList)
        setGroups(groupList)
      } catch (err) {
        if (cancelled) return
        setLoadError(err instanceof ApiError ? err.message : '加载卡片失败')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!menuOpen) return
    const onClick = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) setMenuOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [menuOpen])

  useEffect(() => () => {
    if (savedTimer.current) window.clearTimeout(savedTimer.current)
  }, [])

  const visible = useMemo(() => {
    const needle = keyword.trim().toLowerCase()
    return cards.filter((card) => {
      if (activeGroup && card.group_name !== activeGroup) return false
      if (!needle) return true
      return (
        card.title.toLowerCase().includes(needle) ||
        card.description.toLowerCase().includes(needle) ||
        card.slug.toLowerCase().includes(needle) ||
        card.group_name.toLowerCase().includes(needle)
      )
    })
  }, [cards, keyword, activeGroup])

  const knownGroups = useMemo(
    () => Array.from(new Set(cards.map((card) => card.group_name).filter(Boolean))).sort(),
    [cards],
  )

  function handleOpen(event: ReactMouseEvent<HTMLAnchorElement>, card: Card) {
    // 排序模式下点击只用于拖拽，不触发跳转
    if (reorderMode) {
      event.preventDefault()
      return
    }
    // 挂了环境地址：拦下这次跳转，先让用户选环境。
    // 注意这一下 preventDefault 同时会让中键/Ctrl+点击失效 —— 那是"先确认环境"
    // 的必然代价，选择权交给用户（详见 EndpointPicker 顶部说明）。
    if (card.endpoints.length > 0) {
      event.preventDefault()
      setPicking(card)
      return
    }
    // 跳转交给链接自身的原生行为（不受弹窗拦截影响），这里只做计数与提示
    reportClick(card)
    if (card.open_mode === 'proxy') {
      toast.info(`正在通过跳板机打开「${card.title}」`)
    }
  }

  /** 用户在环境列表里点开某一条 */
  function handleEndpointOpen(card: Card, endpoint: CardEndpoint) {
    reportClick(card, endpoint.slug)
    setPicking(null)
    if (endpoint.open_mode === 'proxy') {
      toast.info(`正在通过跳板机打开「${card.title} · ${endpoint.name}」`)
    }
  }

  async function handleLogout() {
    await logout()
    navigate('/login', { replace: true })
  }

  function toggleReorder() {
    setReorderMode((current) => {
      const next = !current
      if (next) {
        // 排序针对完整列表，先把搜索/分组筛选清掉，避免"只看两三条"时排序结果反直觉
        setKeyword('')
        setActiveGroup('')
      }
      setDragId(null)
      setOverId(null)
      return next
    })
  }

  async function persistOrder(next: Card[]) {
    setSavingOrder(true)
    setOrderSaved(false)
    try {
      await api.reorderCards(next.map((card) => ({ id: card.id, sort_order: card.sort_order })))
      setOrderSaved(true)
      if (savedTimer.current) window.clearTimeout(savedTimer.current)
      savedTimer.current = window.setTimeout(() => setOrderSaved(false), 1800)
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '保存排序失败')
      try {
        await reload()
      } catch {
        // 重新拉取也失败时保留本地顺序，用户可刷新页面
      }
    } finally {
      setSavingOrder(false)
    }
  }

  /**
   * 被拖拽卡片的 id 优先从 dataTransfer 读取，而不是依赖 React state。
   * HTML5 拖放的 dragstart / dragover / drop 可能落在同一个事件循环批次里，
   * 此时 setDragId 还没提交，读 state 会拿到旧值；dataTransfer 是同步可信来源。
   */
  function readDragId(event: DragEvent<HTMLDivElement>): number | null {
    const raw = event.dataTransfer.getData('text/plain')
    const parsed = Number(raw)
    if (Number.isFinite(parsed) && parsed > 0) return parsed
    return dragId
  }

  function handleDragStart(event: DragEvent<HTMLDivElement>, card: Card) {
    setDragId(card.id)
    setOverId(card.id)
    event.dataTransfer.effectAllowed = 'move'
    // Firefox 必须设置数据，否则不触发拖拽
    event.dataTransfer.setData('text/plain', String(card.id))
  }

  function handleDragOver(event: DragEvent<HTMLDivElement>, card: Card) {
    // 必须无条件 preventDefault，否则浏览器不会派发 drop
    event.preventDefault()
    event.dataTransfer.dropEffect = 'move'
    const sourceId = readDragId(event)
    if (sourceId === null || sourceId === card.id) return
    if (overId !== card.id) setOverId(card.id)
  }

  function handleDrop(event: DragEvent<HTMLDivElement>, card: Card) {
    event.preventDefault()
    const sourceId = readDragId(event)
    setDragId(null)
    setOverId(null)
    if (sourceId === null || sourceId === card.id) return

    const from = cards.findIndex((item) => item.id === sourceId)
    const to = cards.findIndex((item) => item.id === card.id)
    if (from < 0 || to < 0) return

    const next = [...cards]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    const renumbered = next.map((item, index) => ({ ...item, sort_order: (index + 1) * SORT_STEP }))

    setCards(renumbered)
    void persistOrder(renumbered)
  }

  function handleDragEnd() {
    setDragId(null)
    setOverId(null)
  }

  async function handleDelete() {
    if (!pendingDelete) return
    setDeleting(true)
    try {
      await api.deleteCard(pendingDelete.id)
      toast.success(`已删除「${pendingDelete.title}」`)
      setPendingDelete(null)
      await reload()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="page">
      <div className="container">
        <header className="page-header">
          <div>
            <h1 className="page-title">开发工具箱</h1>
            <p className="page-subtitle">
              {reorderMode
                ? '拖动卡片调整顺序，松手即自动保存。'
                : '集中管理常用研发工具，点击卡片进入功能页面。'}
            </p>
          </div>

          <div className="header-tools">
            <div className="search-box">
              <SearchIcon size={16} />
              <input
                type="search"
                placeholder="搜索功能卡片"
                value={keyword}
                onChange={(event) => setKeyword(event.target.value)}
                aria-label="搜索功能卡片"
                disabled={reorderMode}
              />
            </div>

            {isAdmin ? (
              <button
                type="button"
                className={`btn ${reorderMode ? 'btn-primary' : 'btn-ghost'}`}
                onClick={toggleReorder}
                disabled={loading || cards.length < 2}
                title={cards.length < 2 ? '卡片不足两张，无需排序' : '拖动卡片调整显示顺序'}
              >
                <SortIcon size={16} />
                {reorderMode ? '完成排序' : '调整顺序'}
              </button>
            ) : null}

            {isAdmin ? (
              <button type="button" className="btn btn-primary" onClick={() => navigate('/admin')}>
                <SettingsIcon size={16} />
                功能管理
              </button>
            ) : null}

            <div ref={menuRef} style={{ position: 'relative' }}>
              <button
                type="button"
                className="btn btn-ghost"
                onClick={() => setMenuOpen((open) => !open)}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
              >
                {user?.display_name || user?.username || '未登录'}
                <span style={{ color: 'var(--ink-400)', fontSize: 11 }}>▾</span>
              </button>

              {menuOpen ? (
                <div
                  role="menu"
                  style={{
                    position: 'absolute',
                    right: 0,
                    top: 46,
                    minWidth: 168,
                    padding: 6,
                    background: 'var(--surface)',
                    border: '1px solid var(--line)',
                    borderRadius: 12,
                    boxShadow: 'var(--shadow-pop)',
                    zIndex: 30,
                  }}
                >
                  <div style={{ padding: '8px 10px 10px', borderBottom: '1px solid var(--line-soft)' }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{user?.display_name || user?.username}</div>
                    <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                      {isAdmin ? '管理员' : '普通用户'}
                    </div>
                  </div>
                  {isAdmin ? (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      style={{ width: '100%', justifyContent: 'flex-start', border: 'none', height: 36 }}
                      onClick={() => {
                        setMenuOpen(false)
                        navigate('/admin')
                      }}
                    >
                      <SettingsIcon size={15} />
                      功能管理
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="btn btn-ghost"
                    style={{ width: '100%', justifyContent: 'flex-start', border: 'none', height: 36 }}
                    onClick={() => void handleLogout()}
                  >
                    <LogoutIcon size={15} />
                    退出登录
                  </button>
                </div>
              ) : null}
            </div>
          </div>
        </header>

        {loadError ? (
          <div className="alert alert-danger" style={{ marginBottom: 20 }}>
            {loadError}
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              style={{ marginLeft: 'auto' }}
              onClick={() => window.location.reload()}
            >
              重新加载
            </button>
          </div>
        ) : null}

        {reorderMode ? (
          <div className="reorder-bar">
            <SortIcon size={16} />
            <span>
              按住卡片拖到目标位置即可调整顺序。当前共 <strong>{cards.length}</strong> 张卡片。
            </span>
            {savingOrder ? <span className="muted">保存中…</span> : null}
            {!savingOrder && orderSaved ? <strong>顺序已保存</strong> : null}
            <button type="button" className="btn btn-sm btn-primary" onClick={toggleReorder}>
              完成
            </button>
          </div>
        ) : groups.length > 1 ? (
          <div className="tabs" role="tablist" aria-label="分组筛选">
            <button
              type="button"
              className={activeGroup === '' ? 'is-active' : ''}
              onClick={() => setActiveGroup('')}
            >
              全部（{cards.length}）
            </button>
            {groups.map((group) => (
              <button
                key={group.name}
                type="button"
                className={activeGroup === group.name ? 'is-active' : ''}
                onClick={() => setActiveGroup(group.name)}
              >
                {group.name}（{group.count}）
              </button>
            ))}
          </div>
        ) : null}

        <div className="card-grid">
          {loading ? (
            <>
              <div className="skeleton" />
              <div className="skeleton" />
              <div className="skeleton" />
            </>
          ) : visible.length === 0 ? (
            <div className="empty">
              <h3>{keyword ? '没有匹配的功能卡片' : '还没有可用的功能卡片'}</h3>
              <p style={{ margin: 0 }}>
                {keyword
                  ? '换个关键词试试，或清空搜索框查看全部功能。'
                  : isAdmin
                    ? '点击右上角「功能管理」添加第一张卡片。'
                    : '请联系管理员配置功能卡片。'}
              </p>
            </div>
          ) : (
            visible.map((card) => (
              <CardTile
                key={card.id}
                card={card}
                onOpen={handleOpen}
                admin={isAdmin && !reorderMode}
                onEdit={setEditing}
                onDelete={setPendingDelete}
                reorderable={reorderMode}
                dragging={dragId === card.id}
                dropBefore={overId === card.id && dragId !== null && dragId !== card.id}
                onDragStart={handleDragStart}
                onDragOver={handleDragOver}
                onDrop={handleDrop}
                onDragEnd={handleDragEnd}
              />
            ))
          )}
        </div>
      </div>

      {editing ? (
        <CardFormModal
          card={editing}
          knownGroups={knownGroups}
          onClose={() => setEditing(null)}
          onSaved={async (message) => {
            toast.success(message)
            setEditing(null)
            try {
              await reload()
            } catch {
              // 保存已成功，仅刷新失败；下次操作会自动重试
            }
          }}
        />
      ) : null}

      {picking ? (
        <EndpointPicker
          card={picking}
          onOpen={handleEndpointOpen}
          onClose={() => setPicking(null)}
        />
      ) : null}

      {pendingDelete ? (
        <ConfirmDialog
          title="删除功能卡片"
          danger
          busy={deleting}
          confirmText="确认删除"
          message={
            <>
              即将删除卡片 <strong>{pendingDelete.title}</strong>（{pendingDelete.slug}）。
              <br />
              该操作不可撤销，删除后门户上将不再显示此功能。
            </>
          }
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void handleDelete()}
        />
      ) : null}
    </div>
  )
}
