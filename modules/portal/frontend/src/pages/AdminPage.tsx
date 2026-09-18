/** 功能管理页：卡片 CRUD + 配置导入导出 + 用户管理。仅管理员可访问。 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { CardFormModal } from '../components/CardFormModal'
import { CardIcon } from '../components/CardIcon'
import { ExportMenu } from '../components/ExportMenu'
import {
  ArrowLeftIcon,
  EditIcon,
  PlusIcon,
  PulseIcon,
  TrashIcon,
  UploadIcon,
} from '../components/Icons'
import { ImportDialog } from '../components/ImportDialog'
import { ConfirmDialog, Modal } from '../components/Modal'
import { useToast } from '../components/Toast'
import { ApiError, api } from '../lib/api'
import { useAuth } from '../lib/auth'
import { buildLaunchTarget } from '../lib/launcher'
import type { Card, ConnectivityResult, ExportFormat, Role, User } from '../lib/types'

export function AdminPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const { user } = useAuth()
  const [tab, setTab] = useState<'cards' | 'users'>('cards')

  return (
    <div className="page">
      <div className="container">
        <header className="page-header">
          <div>
            <h1 className="page-title">功能管理</h1>
            <p className="page-subtitle">维护门户卡片与访问账号，改动即时生效。</p>
          </div>
          <div className="header-tools">
            <button type="button" className="btn btn-ghost" onClick={() => navigate('/')}>
              <ArrowLeftIcon size={16} />
              返回工具箱
            </button>
          </div>
        </header>

        <div className="tabs" role="tablist">
          <button type="button" className={tab === 'cards' ? 'is-active' : ''} onClick={() => setTab('cards')}>
            功能卡片
          </button>
          <button type="button" className={tab === 'users' ? 'is-active' : ''} onClick={() => setTab('users')}>
            用户管理
          </button>
        </div>

        {tab === 'cards' ? <CardManager toast={toast} /> : <UserManager toast={toast} currentUid={user?.uid ?? ''} />}
      </div>
    </div>
  )
}

// ======================================================================
// 卡片管理
// ======================================================================
function CardManager({ toast }: { toast: ReturnType<typeof useToast> }) {
  const [cards, setCards] = useState<Card[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<Card | null>(null)
  const [creating, setCreating] = useState(false)
  const [pendingDelete, setPendingDelete] = useState<Card | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [checkingId, setCheckingId] = useState<number | null>(null)
  const [checkResult, setCheckResult] = useState<{ card: Card; result: ConnectivityResult } | null>(null)
  const [exporting, setExporting] = useState(false)
  const [importFile, setImportFile] = useState<File | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  const knownGroups = useMemo(
    () => Array.from(new Set(cards.map((card) => card.group_name).filter(Boolean))).sort(),
    [cards],
  )

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setCards(await api.listCards({ include_disabled: true }))
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function handleCheck(card: Card) {
    setCheckingId(card.id)
    try {
      const result = await api.checkCard(card.id)
      setCheckResult({ card, result })
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '自检失败')
    } finally {
      setCheckingId(null)
    }
  }

  async function handleDelete() {
    if (!pendingDelete) return
    setDeleting(true)
    try {
      await api.deleteCard(pendingDelete.id)
      toast.success(`已删除「${pendingDelete.title}」`)
      setPendingDelete(null)
      await load()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败')
    } finally {
      setDeleting(false)
    }
  }

  async function handleExport(format: ExportFormat) {
    setExporting(true)
    try {
      const { count } = await api.exportCards(format)
      toast.success(
        count > 0 ? `已导出 ${count} 张卡片（${format.toUpperCase()}）` : `已导出空配置（${format.toUpperCase()}）`,
      )
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '导出失败')
    } finally {
      setExporting(false)
    }
  }

  function handleImportFile(file: File | undefined) {
    if (!file) return
    // 立刻清空 input 的值，否则连续选同一个文件不会再触发 change
    if (fileRef.current) fileRef.current.value = ''
    // 文件内容交给对话框去上传并由服务端解析，这里只留下引用
    setImportFile(file)
  }

  return (
    <>
      <div className="toolbar-row">
        <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
          <PlusIcon size={16} />
          新建功能卡片
        </button>
        <div className="spacer" />
        <ExportMenu busy={exporting} onPick={(format) => void handleExport(format)} />
        <button type="button" className="btn btn-ghost" onClick={() => fileRef.current?.click()}>
          <UploadIcon size={15} />
          导入配置
        </button>
        <input
          ref={fileRef}
          className="visually-hidden"
          type="file"
          accept=".xlsx,.xlsm,.xls,.csv,.json,application/json,text/csv"
          onChange={(event) => handleImportFile(event.target.files?.[0])}
        />
      </div>

      {error ? (
        <div className="alert alert-danger" style={{ marginBottom: 16 }}>
          {error}
        </div>
      ) : null}

      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th style={{ width: 64 }}>排序</th>
              <th>功能</th>
              <th style={{ width: 92 }}>访问方式</th>
              <th style={{ width: 92 }}>环境地址</th>
              <th>地址</th>
              <th style={{ width: 96 }}>分组</th>
              <th style={{ width: 76 }}>状态</th>
              <th style={{ width: 84 }}>打开次数</th>
              <th style={{ width: 250 }} />
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={9} style={{ padding: 32, textAlign: 'center' }} className="muted">
                  加载中…
                </td>
              </tr>
            ) : cards.length === 0 ? (
              <tr>
                <td colSpan={9} style={{ padding: 40, textAlign: 'center' }} className="muted">
                  暂无卡片，点击右上角「新建功能卡片」开始配置。
                </td>
              </tr>
            ) : (
              cards.map((card) => (
                <tr key={card.id}>
                  <td className="mono">{card.sort_order}</td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                      <CardIcon card={card} size={32} />
                      <div style={{ minWidth: 0 }}>
                        <div style={{ fontWeight: 600 }}>{card.title}</div>
                        <div className="muted mono" style={{ fontSize: 11.5 }}>
                          {card.slug}
                        </div>
                      </div>
                    </div>
                  </td>
                  <td>
                    <span className={`badge ${card.open_mode === 'proxy' ? '' : 'badge-direct'}`}>
                      {card.open_mode === 'proxy' ? '代理' : '直连'}
                    </span>
                  </td>
                  <td>
                    {card.endpoints.length > 0 ? (
                      <span
                        className="badge badge-env"
                        title={card.endpoints
                          .map(
                            (endpoint) =>
                              `${endpoint.name}（${endpoint.open_mode === 'proxy' ? '代理' : '直连'}）\n${endpoint.url}`,
                          )
                          .join('\n\n')}
                      >
                        {card.endpoints.length} 条
                      </span>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>
                    <span className="mono truncate" title={card.target_url}>
                      {card.target_url}
                    </span>
                  </td>
                  <td>{card.group_name}</td>
                  <td>
                    <span className={`badge ${card.enabled ? 'badge-success' : 'badge-off'}`}>
                      {card.enabled ? '启用' : '停用'}
                    </span>
                  </td>
                  <td className="mono">{card.click_count}</td>
                  <td>
                    <div className="cell-actions">
                      <button
                        type="button"
                        className="btn btn-sm btn-ghost"
                        onClick={() => void handleCheck(card)}
                        disabled={checkingId === card.id}
                        title="从跳板机探测目标是否可达"
                      >
                        <PulseIcon size={14} />
                        {checkingId === card.id ? '探测中' : '自检'}
                      </button>
                      <a
                        className="btn btn-sm btn-ghost"
                        href={buildLaunchTarget(card).url}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="在新标签页打开（链接导航，不受弹窗拦截影响）"
                      >
                        预览
                      </a>
                      <button type="button" className="btn btn-sm btn-ghost" onClick={() => setEditing(card)}>
                        <EditIcon size={14} />
                        编辑
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        onClick={() => setPendingDelete(card)}
                        aria-label={`删除 ${card.title}`}
                      >
                        <TrashIcon size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {(creating || editing) ? (
        <CardFormModal
          card={editing}
          knownGroups={knownGroups}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
          onSaved={async (message) => {
            toast.success(message)
            setCreating(false)
            setEditing(null)
            await load()
          }}
        />
      ) : null}

      {importFile ? (
        <ImportDialog
          file={importFile}
          existingCount={cards.length}
          onClose={() => setImportFile(null)}
          onDone={async (message) => {
            toast.success(message)
            setImportFile(null)
            await load()
          }}
          onError={(message) => toast.error(message)}
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

      {checkResult ? (
        <Modal
          title="连通性自检结果"
          subtitle={`从跳板机探测 ${checkResult.card.target_url}`}
          onClose={() => setCheckResult(null)}
          width={520}
          footer={
            <button type="button" className="btn btn-primary" onClick={() => setCheckResult(null)}>
              知道了
            </button>
          }
        >
          <div className={`alert ${checkResult.result.reachable ? 'alert-success' : 'alert-danger'}`}>
            <div>
              <strong>{checkResult.result.reachable ? '目标可达' : '目标不可达'}</strong>
              <div style={{ marginTop: 4 }}>{checkResult.result.message}</div>
              {checkResult.result.hint ? <div style={{ marginTop: 4 }}>{checkResult.result.hint}</div> : null}
            </div>
          </div>
          {checkResult.result.reachable ? (
            <dl style={{ margin: '16px 0 0', fontSize: 13, lineHeight: 2 }}>
              <div style={{ display: 'flex', gap: 12 }}>
                <dt className="muted" style={{ width: 96 }}>
                  HTTP 状态
                </dt>
                <dd className="mono" style={{ margin: 0 }}>
                  {checkResult.result.status_code}
                </dd>
              </div>
              <div style={{ display: 'flex', gap: 12 }}>
                <dt className="muted" style={{ width: 96 }}>
                  首次响应
                </dt>
                <dd className="mono" style={{ margin: 0 }}>
                  {checkResult.result.latency_ms} ms
                </dd>
              </div>
              <div style={{ display: 'flex', gap: 12 }}>
                <dt className="muted" style={{ width: 96 }}>
                  Server
                </dt>
                <dd className="mono" style={{ margin: 0 }}>
                  {checkResult.result.server || '（未返回）'}
                </dd>
              </div>
            </dl>
          ) : null}
        </Modal>
      ) : null}
    </>
  )
}


// ======================================================================
// 用户管理
// ======================================================================
function UserManager({ toast, currentUid }: { toast: ReturnType<typeof useToast>; currentUid: string }) {
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<User | null>(null)
  const [pendingDelete, setPendingDelete] = useState<User | null>(null)
  const [deleting, setDeleting] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setUsers(await api.listUsers())
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  async function toggleActive(user: User) {
    try {
      await api.updateUser(user.uid, { is_active: !user.is_active })
      toast.success(`${user.username} 已${user.is_active ? '停用' : '启用'}`)
      await load()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '操作失败')
    }
  }

  async function handleDelete() {
    if (!pendingDelete) return
    setDeleting(true)
    try {
      await api.deleteUser(pendingDelete.uid)
      toast.success(`已删除用户 ${pendingDelete.username}`)
      setPendingDelete(null)
      await load()
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 14 }}>
        <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
          <PlusIcon size={16} />
          新建用户
        </button>
      </div>

      {error ? (
        <div className="alert alert-danger" style={{ marginBottom: 16 }}>
          {error}
        </div>
      ) : null}

      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>用户名</th>
              <th>显示名称</th>
              <th style={{ width: 100 }}>角色</th>
              <th style={{ width: 90 }}>状态</th>
              <th style={{ width: 150 }}>最近登录</th>
              <th style={{ width: 210 }} />
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={6} style={{ padding: 32, textAlign: 'center' }} className="muted">
                  加载中…
                </td>
              </tr>
            ) : (
              users.map((user) => (
                <tr key={user.uid}>
                  <td className="mono" style={{ fontWeight: 600 }}>
                    {user.username}
                    {user.uid === currentUid ? <span className="badge badge-muted" style={{ marginLeft: 8 }}>当前账号</span> : null}
                  </td>
                  <td>{user.display_name || '—'}</td>
                  <td>
                    <span className={`badge ${user.role === 'admin' ? '' : 'badge-muted'}`}>
                      {user.role === 'admin' ? '管理员' : '普通用户'}
                    </span>
                  </td>
                  <td>
                    <span className={`badge ${user.is_active ? 'badge-success' : 'badge-off'}`}>
                      {user.is_active ? '正常' : '已停用'}
                    </span>
                  </td>
                  <td className="mono muted" style={{ fontSize: 12 }}>
                    {user.last_login_at ? user.last_login_at.replace('T', ' ').slice(0, 16) : '从未登录'}
                  </td>
                  <td>
                    <div className="cell-actions">
                      <button type="button" className="btn btn-sm btn-ghost" onClick={() => setEditing(user)}>
                        <EditIcon size={14} />
                        编辑
                      </button>
                      <button type="button" className="btn btn-sm btn-ghost" onClick={() => void toggleActive(user)}>
                        {user.is_active ? '停用' : '启用'}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        onClick={() => setPendingDelete(user)}
                        disabled={user.uid === currentUid}
                        aria-label={`删除 ${user.username}`}
                      >
                        <TrashIcon size={14} />
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {(creating || editing) ? (
        <UserFormModal
          user={editing}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
          onSaved={async (message) => {
            toast.success(message)
            setCreating(false)
            setEditing(null)
            await load()
          }}
        />
      ) : null}

      {pendingDelete ? (
        <ConfirmDialog
          title="删除用户"
          danger
          busy={deleting}
          confirmText="确认删除"
          message={
            <>
              即将删除用户 <strong>{pendingDelete.username}</strong>，该账号将立即无法登录。
              <br />
              系统会保留至少一个启用状态的管理员，最后一管理员无法删除。
            </>
          }
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void handleDelete()}
        />
      ) : null}
    </>
  )
}

function UserFormModal({
  user,
  onClose,
  onSaved,
}: {
  user: User | null
  onClose: () => void
  onSaved: (message: string) => void | Promise<void>
}) {
  const isEdit = Boolean(user)
  const [username, setUsername] = useState(user?.username ?? '')
  const [displayName, setDisplayName] = useState(user?.display_name ?? '')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<Role>(user?.role ?? 'user')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError('')

    if (!isEdit && password.length < 8) {
      setError('密码至少 8 位')
      return
    }
    if (isEdit && password && password.length < 8) {
      setError('新密码至少 8 位')
      return
    }

    setBusy(true)
    try {
      if (isEdit && user) {
        await api.updateUser(user.uid, {
          display_name: displayName,
          role,
          ...(password ? { password } : {}),
        })
        await onSaved(`已更新用户 ${user.username}`)
      } else {
        await api.createUser({ username: username.trim(), password, display_name: displayName, role })
        await onSaved(`已创建用户 ${username.trim()}`)
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={isEdit ? `编辑用户 ${user?.username}` : '新建用户'}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" form="user-form" className="btn btn-primary" disabled={busy}>
            {busy ? <span className="spinner" /> : null}
            {busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <form id="user-form" onSubmit={submit}>
        {error ? (
          <div className="alert alert-danger" style={{ marginBottom: 16 }}>
            {error}
          </div>
        ) : null}

        <div className="field">
          <label htmlFor="u-name">用户名</label>
          <input
            id="u-name"
            className="input mono"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={isEdit}
            required
            minLength={2}
          />
          {isEdit ? <span className="hint">用户名创建后不可修改。</span> : null}
        </div>

        <div className="field">
          <label htmlFor="u-display">显示名称</label>
          <input
            id="u-display"
            className="input"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            placeholder="用于页面右上角展示"
          />
        </div>

        <div className="field">
          <label htmlFor="u-pass">{isEdit ? '重置密码（留空则不修改）' : '初始密码'}</label>
          <input
            id="u-pass"
            className="input"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required={!isEdit}
            minLength={isEdit ? undefined : 8}
          />
          <span className="hint">至少 8 位。令牌不会下发到前端，登录态走 HttpOnly Cookie。</span>
        </div>

        <div className="field">
          <label htmlFor="u-role">角色</label>
          <select
            id="u-role"
            className="select"
            value={role}
            onChange={(event) => setRole(event.target.value as Role)}
          >
            <option value="user">普通用户（只读卡片）</option>
            <option value="admin">管理员（可维护卡片与用户）</option>
          </select>
        </div>
      </form>
    </Modal>
  )
}
