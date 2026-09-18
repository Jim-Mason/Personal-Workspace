/** 与后端 Pydantic schema 一一对应的类型定义。 */

export type Role = 'admin' | 'user'
export type OpenMode = 'direct' | 'proxy'

/** 卡片底色预设，空串表示跟随默认（蓝色渐变） */
export type BgStyle = '' | 'default' | 'blue' | 'violet' | 'mint' | 'sand' | 'rose' | 'slate'

export interface User {
  uid: string
  username: string
  display_name: string
  role: Role
  is_active: boolean
  last_login_at: string | null
  created_at: string
}

/**
 * 卡片下的一条环境地址（生产 / 测试 / 预发…）。
 *
 * `slug` 是它在网关里的入口标识（`/gw/{slug}/`），与卡片的 slug **共享同一个
 * 命名空间**；后端新建时自动生成，前端编辑已有条目时要把它原样回传，
 * 这样改名字不会把已经发出去的书签链接改掉。
 */
export interface CardEndpoint {
  id: number
  card_id: number
  slug: string
  name: string
  url: string
  /** 每条地址自带打开方式，所以同一张卡片可以「生产走代理、内网测试直连」 */
  open_mode: OpenMode
  verify_tls: boolean | null
  sort_order: number
  /** 后端计算的入口地址：proxy 为 /gw/{slug}/，direct 为 url 本身 */
  launch_url: string
}

/** 提交环境地址时的形状（不含后端算出来的字段） */
export interface CardEndpointPayload {
  /** 已有条目的标识，原样回传；新增时留空由后端生成 */
  slug?: string
  name: string
  url: string
  open_mode: OpenMode
  verify_tls?: boolean | null
}

export interface Card {
  id: number
  slug: string
  title: string
  description: string
  icon: string
  icon_style: string
  /** 自定义图片图标：/uploads/icons/xxx.png 或 http(s) 外链；空串表示用文字图标 */
  icon_url: string
  /** 卡片底色预设 */
  bg_style: BgStyle
  /** 图标主色 #rrggbb；空串表示跟随底色预设 */
  accent_color: string
  target_url: string
  open_mode: OpenMode
  group_name: string
  sort_order: number
  enabled: boolean
  open_in_new_tab: boolean
  click_count: number
  verify_tls: boolean | null
  created_at: string
  updated_at: string
  /** 后端计算的入口地址：proxy 模式为 /gw/{slug}/，direct 模式为原始地址 */
  launch_url: string
  /**
   * 该卡片挂的环境地址。
   *
   * 非空时点卡片**不直接跳转**，而是弹出列表让用户挑一个（一个图标归档整条业务线）。
   * 空数组表示这张卡片就是单地址，点开即走 launch_url —— 与加这个功能之前完全一致。
   */
  endpoints: CardEndpoint[]
}

export interface CardGroup {
  name: string
  count: number
}

export interface Session {
  user: User
  access_expires_at: string
  refresh_expires_at: string
}

export interface CardPayload {
  title: string
  description?: string
  icon?: string
  icon_style?: string
  icon_url?: string
  bg_style?: BgStyle
  accent_color?: string
  target_url: string
  open_mode: OpenMode
  group_name?: string
  sort_order?: number
  enabled?: boolean
  open_in_new_tab?: boolean
  verify_tls?: boolean | null
  slug?: string
  /**
   * 环境地址。
   *
   * 语义是**整体替换**：编辑器整份列表提交，后端按 slug 认领已有条目、只增删差异。
   * 字段**不带** = 不动（PATCH 局部更新时用），带空数组 = 清空。
   */
  endpoints?: CardEndpointPayload[]
}

export interface ConnectivityResult {
  reachable: boolean
  stage: string
  /** 探测的目标：卡片本体是 slug，环境地址是 `卡片slug/环境slug` */
  target?: string
  status_code?: number
  latency_ms?: number
  server?: string
  content_type?: string
  message: string
  hint?: string
}

/** 图标上传结果 */
export interface IconUploadResult {
  url: string
  filename: string
  size: number
  content_type: string
}

/**
 * 导出格式。
 *
 * 三种格式的列定义都来自后端 `card_sheet.CARD_COLUMNS`：
 * - `xlsx`：带说明页与下拉，适合人工成批改
 * - `csv`：纯文本，带 UTF-8 BOM，Excel 双击不乱码
 * - `json`：完整结构（含 version/exported_at），历史默认，供脚本使用
 */
export type ExportFormat = 'json' | 'xlsx' | 'csv'

export type ImportMode = 'merge' | 'replace'

export interface CardImportResult {
  mode: string
  dry_run: boolean
  created: number
  updated: number
  deleted: number
  skipped: number
  total: number
  errors: string[]
}

export interface AuditLog {
  id: number
  username: string
  action: string
  target_type: string
  target_id: string
  detail: string
  ip: string
  created_at: string
}
