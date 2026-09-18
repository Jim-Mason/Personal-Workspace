/**
 * 类型化 API 客户端。
 *
 * 设计要点：
 * - 令牌存于 HttpOnly Cookie，JS 拿不到，因此所有请求都带 credentials: 'include'
 * - 401 时自动用 refresh 换一次新令牌并重试原请求，前端无需处理续期
 * - 后端统一错误体 { error: { code, message, details } } 映射为 ApiError
 * - 4xx 不重试；5xx/网络错误最多重试 3 次（指数退避）
 */

import type {
  AuditLog,
  Card,
  CardGroup,
  CardImportResult,
  CardPayload,
  ConnectivityResult,
  ExportFormat,
  IconUploadResult,
  ImportMode,
  Role,
  Session,
  User,
} from './types'

const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details?: unknown

  constructor(message: string, status: number, code: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }

  /** 是否为"未登录/登录过期"，调用方据此跳登录页 */
  get isAuthError(): boolean {
    return this.status === 401
  }
}

const HTTP_FALLBACK_MESSAGE: Record<number, string> = {
  400: '请求参数不合法',
  401: '登录已过期，请重新登录',
  403: '没有权限执行该操作',
  404: '请求的资源不存在',
  409: '资源冲突，请刷新后重试',
  413: '上传内容过大，已超过服务端限制',
  422: '提交的内容未通过校验',
  429: '操作过于频繁，请稍后再试',
  502: '目标内网服务不可达',
  503: '服务暂时不可用',
}

interface RequestOptions {
  method?: string
  body?: unknown
  query?: Record<string, string | number | boolean | undefined | null>
  /** 内部使用：标记这是刷新令牌后的重试，避免无限递归 */
  _retried?: boolean
  /** 内部使用：跳过刷新逻辑（登录/刷新接口自身） */
  _skipRefresh?: boolean
}

let refreshInFlight: Promise<boolean> | null = null

async function refreshSession(): Promise<boolean> {
  // 多个请求同时 401 时只发一次刷新请求
  if (!refreshInFlight) {
    refreshInFlight = fetch(`${BASE}/api/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
      headers: { Accept: 'application/json' },
    })
      .then((response) => response.ok)
      .catch(() => false)
      .finally(() => {
        refreshInFlight = null
      })
  }
  return refreshInFlight
}

async function parseError(response: Response): Promise<ApiError> {
  let code = 'http_error'
  let message = HTTP_FALLBACK_MESSAGE[response.status] ?? `请求失败（HTTP ${response.status}）`
  let details: unknown

  try {
    const payload = await response.json()
    if (payload?.error) {
      code = payload.error.code ?? code
      message = payload.error.message ?? message
      details = payload.error.details
      if (Array.isArray(details) && details.length > 0) {
        const first = details[0] as { field?: string; reason?: string }
        if (first?.reason) {
          message = first.field ? `${first.field}：${first.reason}` : first.reason
        }
      }
    }
  } catch {
    // 响应体不是 JSON，沿用兜底文案
  }
  return new ApiError(message, response.status, code, details)
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, _retried = false, _skipRefresh = false } = options

  let url = `${BASE}${path}`
  if (query) {
    const params = new URLSearchParams()
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
    }
    const qs = params.toString()
    if (qs) url += `?${qs}`
  }

  const headers: Record<string, string> = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  const maxAttempts = method === 'GET' ? 3 : 1
  let attempt = 0

  for (;;) {
    attempt += 1
    try {
      response = await fetch(url, {
        method,
        credentials: 'include',
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch {
      // 网络层失败（离线 / DNS / 连接被拒）
      if (attempt < maxAttempts) {
        await sleep(300 * attempt)
        continue
      }
      throw new ApiError('网络连接失败，请检查网络或稍后重试', 0, 'network_error')
    }

    if (response.status >= 500 && attempt < maxAttempts) {
      await sleep(300 * attempt)
      continue
    }
    break
  }

  if (response.status === 401 && !_retried && !_skipRefresh) {
    const refreshed = await refreshSession()
    if (refreshed) {
      return request<T>(path, { ...options, _retried: true })
    }
  }

  if (!response.ok) {
    throw await parseError(response)
  }

  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) return (await response.text()) as unknown as T
  return (await response.json()) as T
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * multipart 专用：复用同样的 401 续期与错误映射，但 body 不做 JSON 序列化。
 * 注意不要手动设置 Content-Type —— 需要浏览器自动补上 boundary。
 */
async function requestMultipart<T>(path: string, form: FormData, method = 'POST'): Promise<T> {
  const send = () =>
    fetch(`${BASE}${path}`, { method, credentials: 'include', body: form })

  let response: Response
  try {
    response = await send()
  } catch {
    throw new ApiError('网络连接失败，请检查网络或稍后重试', 0, 'network_error')
  }

  if (response.status === 401) {
    const refreshed = await refreshSession()
    if (refreshed) {
      try {
        response = await send()
      } catch {
        throw new ApiError('网络连接失败，请检查网络或稍后重试', 0, 'network_error')
      }
    }
  }

  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** 触发浏览器下载 */
function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // 立刻 revoke 会让部分浏览器来不及开始下载，延后回收
  window.setTimeout(() => URL.revokeObjectURL(url), 2000)
}

// ----------------------------------------------------------------------
// 业务接口
// ----------------------------------------------------------------------
export const api = {
  // 认证
  login: (username: string, password: string) =>
    request<Session>('/api/auth/login', {
      method: 'POST',
      body: { username, password },
      _skipRefresh: true,
    }),
  logout: () => request<{ message: string }>('/api/auth/logout', { method: 'POST', _skipRefresh: true }),
  me: () => request<User>('/api/auth/me', { _skipRefresh: true }),
  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ message: string }>('/api/auth/password', {
      method: 'POST',
      body: { old_password: oldPassword, new_password: newPassword },
    }),

  // 卡片
  listCards: (params: { keyword?: string; group_name?: string; include_disabled?: boolean } = {}) =>
    request<Card[]>('/api/cards', { query: params }),
  listGroups: () => request<CardGroup[]>('/api/cards/groups'),
  createCard: (payload: CardPayload) => request<Card>('/api/cards', { method: 'POST', body: payload }),
  updateCard: (id: number, payload: Partial<CardPayload>) =>
    request<Card>(`/api/cards/${id}`, { method: 'PATCH', body: payload }),
  deleteCard: (id: number) => request<{ message: string }>(`/api/cards/${id}`, { method: 'DELETE' }),
  reorderCards: (items: Array<{ id: number; sort_order: number }>) =>
    request<{ message: string }>('/api/cards/reorder', { method: 'POST', body: { items } }),
  /**
   * 记录一次打开。`endpointSlug` 是点开的那条环境地址（点卡片本体时省略）。
   *
   * 实际调用走 `lib/launcher.ts` 的 `reportClick`（那里带 keepalive，页面跳走也能送达），
   * 这个包装留给需要在页面内等待结果的场景。
   */
  recordClick: (id: number, endpointSlug?: string) =>
    request<{ message: string }>(`/api/cards/${id}/click`, {
      method: 'POST',
      query: endpointSlug ? { endpoint: endpointSlug } : undefined,
    }),
  /** 连通性自检。带 `endpointSlug` 时探测那条环境地址，并按它自己的证书策略判断 */
  checkCard: (id: number, endpointSlug?: string) =>
    request<ConnectivityResult>(`/api/cards/${id}/check`, {
      method: 'POST',
      query: endpointSlug ? { endpoint: endpointSlug } : undefined,
    }),

  // 图标上传
  uploadIcon: (file: File) => {
    const form = new FormData()
    form.append('file', file, file.name)
    return requestMultipart<IconUploadResult>('/api/uploads/icon', form)
  },

  // 配置导入导出
  /**
   * 导出并直接触发浏览器下载，返回文件名与卡片数量供提示使用。
   *
   * 数量走 `X-Card-Count` 响应头而不是解析响应体：三种格式的响应体分别是
   * JSON / CSV 文本 / 二进制 xlsx，为了拿一个数字去解析它们既慢又容易出错。
   */
  exportCards: async (
    format: ExportFormat = 'json',
  ): Promise<{ filename: string; count: number }> => {
    const response = await fetch(`${BASE}/api/cards/export?format=${format}`, {
      credentials: 'include',
    })
    if (!response.ok) throw await parseError(response)

    const disposition = response.headers.get('content-disposition') ?? ''
    const matched = /filename="?([^";]+)"?/i.exec(disposition)
    const filename = matched?.[1] ?? `dev-toolbox-cards-${Date.now()}.${format}`
    const count = Number(response.headers.get('x-card-count') ?? '') || 0

    // 用 response.blob() 而不是自己 new Blob：这样 Content-Type 与 BOM 字节都原样保留
    downloadBlob(await response.blob(), filename)
    return { filename, count }
  },

  /**
   * 上传表格 / JSON 文件导入。
   *
   * 解析放在服务端：GBK 编码、Excel 把地址认成日期、以「=」开头的文本被当公式
   * 这些坑都在 `card_sheet` 那一侧统一处理，前端只负责把文件原样传上去。
   * 先用 `dryRun` 预演、确认后再真正导入，两次调用同一个接口。
   */
  importCardsFile: (file: File, mode: ImportMode, dryRun = false) => {
    const form = new FormData()
    form.append('file', file, file.name)
    form.append('mode', mode)
    form.append('dry_run', String(dryRun))
    return requestMultipart<CardImportResult>('/api/cards/import/file', form)
  },

  // 用户
  listUsers: () => request<User[]>('/api/users'),
  createUser: (payload: { username: string; password: string; display_name?: string; role: Role }) =>
    request<User>('/api/users', { method: 'POST', body: payload }),
  updateUser: (uid: string, payload: Partial<{ password: string; display_name: string; role: Role; is_active: boolean }>) =>
    request<User>(`/api/users/${uid}`, { method: 'PATCH', body: payload }),
  deleteUser: (uid: string) => request<{ message: string }>(`/api/users/${uid}`, { method: 'DELETE' }),
}

export type { AuditLog }
