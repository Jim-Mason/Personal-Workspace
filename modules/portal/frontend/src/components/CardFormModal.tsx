/**
 * 卡片编辑表单（首页与功能管理页共用）。
 *
 * 外观部分提供三件事：图片图标上传、底色预设、图标主色。
 * 因为没有实时预览很难判断效果，表单顶部放了一个迷你磁贴预览。
 */

import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  ACCENT_PRESETS,
  BG_STYLE_OPTIONS,
  ICON_STYLE_OPTIONS,
  isValidAccent,
  safeIconSrc,
} from '../lib/appearance'
import { ApiError, api } from '../lib/api'
import type { Card, CardEndpointPayload, CardPayload, OpenMode } from '../lib/types'
import { CardIcon } from './CardIcon'
import { GroupPicker } from './GroupPicker'
import {
  ImageIcon,
  PaletteIcon,
  PlusIcon,
  RestoreIcon,
  TrashIcon,
  UploadIcon,
} from './Icons'
import { Modal } from './Modal'

const MAX_ICON_BYTES = 512 * 1024
const ACCEPT_ICON = 'image/png,image/jpeg,image/webp,image/gif,image/x-icon,image/bmp'

/** 与后端 `MAX_CARD_ENDPOINTS` 保持一致；两处都拦，前端先给出人话提示 */
const MAX_ENDPOINTS = 20

export const EMPTY_CARD_FORM: CardPayload = {
  title: '',
  description: '',
  icon: '',
  icon_style: 'blue',
  icon_url: '',
  bg_style: '',
  accent_color: '',
  target_url: 'http://',
  open_mode: 'proxy',
  group_name: '默认分组',
  sort_order: 0,
  enabled: true,
  open_in_new_tab: true,
  verify_tls: null,
  endpoints: [],
}

/**
 * 卡片 → 表单。
 *
 * 环境地址要**带上 slug 原样回传**：后端靠它认领已有记录，这样改个名字或调整顺序
 * 不会把那条地址的入口标识换掉（换了就等于已分享的书签链接失效）。
 */
function toForm(card: Card | null): CardPayload {
  if (!card) return { ...EMPTY_CARD_FORM, endpoints: [] }
  return {
    title: card.title,
    description: card.description,
    icon: card.icon,
    icon_style: card.icon_style,
    icon_url: card.icon_url,
    bg_style: card.bg_style,
    accent_color: card.accent_color,
    target_url: card.target_url,
    open_mode: card.open_mode,
    group_name: card.group_name,
    sort_order: card.sort_order,
    enabled: card.enabled,
    open_in_new_tab: card.open_in_new_tab,
    verify_tls: card.verify_tls,
    endpoints: card.endpoints.map((endpoint) => ({
      slug: endpoint.slug,
      name: endpoint.name,
      url: endpoint.url,
      open_mode: endpoint.open_mode,
      // 界面上没有这个开关，但要原样带回去 —— 否则一次"改个名字"的保存
      // 会把历史上设过的证书策略悄悄清成默认值
      verify_tls: endpoint.verify_tls,
    })),
  }
}

interface CardFormModalProps {
  card: Card | null
  onClose: () => void
  onSaved: (message: string) => void | Promise<void>
  onError?: (message: string) => void
  /** 已知分组，用于下拉候选与快捷选择 chip */
  knownGroups?: string[]
}

export function CardFormModal({ card, onClose, onSaved, onError, knownGroups = [] }: CardFormModalProps) {
  const isEdit = Boolean(card)
  const [form, setForm] = useState<CardPayload>(() => toForm(card))
  const [slug, setSlug] = useState(card?.slug ?? '')
  const [busy, setBusy] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [fieldError, setFieldError] = useState('')
  const fileRef = useRef<HTMLInputElement | null>(null)

  function patch(changes: Partial<CardPayload>) {
    setForm((current) => ({ ...current, ...changes }))
  }

  // ------------------------------------------------------------------
  // 环境地址列表：整份提交，后端按 slug 认领已有条目、只增删差异
  // ------------------------------------------------------------------
  function patchEndpoint(index: number, changes: Partial<CardEndpointPayload>) {
    setForm((current) => ({
      ...current,
      endpoints: (current.endpoints ?? []).map((item, position) =>
        position === index ? { ...item, ...changes } : item,
      ),
    }))
  }

  function addEndpoint() {
    setForm((current) => {
      const list = current.endpoints ?? []
      if (list.length >= MAX_ENDPOINTS) return current
      return {
        ...current,
        // 新行的打开方式直接继承卡片当前的选择：多数场景下同一套系统的
        // 各个环境访问方式是一致的，省一次点击
        endpoints: [
          ...list,
          { name: '', url: 'http://', open_mode: current.open_mode ?? 'direct' },
        ],
      }
    })
  }

  function removeEndpoint(index: number) {
    setForm((current) => ({
      ...current,
      endpoints: (current.endpoints ?? []).filter((_, position) => position !== index),
    }))
  }

  function moveEndpoint(index: number, delta: number) {
    setForm((current) => {
      const list = [...(current.endpoints ?? [])]
      const target = index + delta
      if (target < 0 || target >= list.length) return current
      ;[list[index], list[target]] = [list[target], list[index]]
      return { ...current, endpoints: list }
    })
  }

  const endpoints = form.endpoints ?? []

  /** 校验并归一化环境地址；返回 null 表示校验未通过（错误已写进 fieldError）。 */
  function normalizeEndpoints(): CardEndpointPayload[] | null {
    if (endpoints.length > MAX_ENDPOINTS) {
      setFieldError(`一张卡片最多 ${MAX_ENDPOINTS} 条环境地址，当前 ${endpoints.length} 条`)
      return null
    }
    const cleaned = endpoints.map((endpoint) => ({
      ...endpoint,
      name: endpoint.name.trim(),
      url: endpoint.url.trim(),
    }))
    for (const [index, endpoint] of cleaned.entries()) {
      if (!endpoint.name) {
        setFieldError(`第 ${index + 1} 条环境地址还没填名称`)
        return null
      }
      if (!/^https?:\/\//i.test(endpoint.url)) {
        setFieldError(`环境「${endpoint.name}」的地址必须以 http:// 或 https:// 开头`)
        return null
      }
    }
    const names = cleaned.map((endpoint) => endpoint.name.toLowerCase())
    if (new Set(names).size !== names.length) {
      setFieldError('环境名称不能重复，否则弹窗里分不清该点哪一个')
      return null
    }
    return cleaned
  }

  async function handleIconFile(file: File | undefined) {
    if (!file) return
    setFieldError('')

    if (file.size > MAX_ICON_BYTES) {
      const message = `图标不能超过 ${MAX_ICON_BYTES / 1024} KB，当前 ${Math.round(file.size / 1024)} KB`
      setFieldError(message)
      onError?.(message)
      return
    }

    setUploading(true)
    try {
      const result = await api.uploadIcon(file)
      patch({ icon_url: result.url })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : '图标上传失败'
      setFieldError(message)
      onError?.(message)
    } finally {
      setUploading(false)
      // 允许重复选择同一个文件
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    setFieldError('')

    const title = form.title.trim()
    const targetUrl = form.target_url.trim()
    if (!title) {
      setFieldError('请填写功能名称')
      return
    }
    if (!/^https?:\/\//i.test(targetUrl)) {
      setFieldError('目标地址必须以 http:// 或 https:// 开头')
      return
    }
    if (form.accent_color && !isValidAccent(form.accent_color)) {
      setFieldError('图标主色必须是 #RRGGBB 格式，例如 #2b6cf6')
      return
    }

    const normalizedEndpoints = normalizeEndpoints()
    if (normalizedEndpoints === null) return

    setBusy(true)
    try {
      if (isEdit && card) {
        const payload: Partial<CardPayload> = {
          ...form,
          title,
          target_url: targetUrl,
          endpoints: normalizedEndpoints,
        }
        delete payload.slug
        await api.updateCard(card.id, payload)
        await onSaved(`已更新「${title}」`)
      } else {
        await api.createCard({
          ...form,
          title,
          target_url: targetUrl,
          endpoints: normalizedEndpoints,
          ...(slug.trim() ? { slug: slug.trim().toLowerCase() } : {}),
        })
        await onSaved(`已创建「${title}」`)
      }
    } catch (err) {
      const message = err instanceof ApiError ? err.message : '保存失败'
      setFieldError(message)
      onError?.(message)
    } finally {
      setBusy(false)
    }
  }

  const iconSrc = safeIconSrc(form.icon_url ?? '')
  const previewCard = {
    icon: form.icon ?? '',
    icon_style: form.icon_style ?? 'blue',
    icon_url: form.icon_url ?? '',
    accent_color: form.accent_color ?? '',
    title: form.title || '功能名称',
  }

  return (
    <Modal
      title={isEdit ? `编辑「${card?.title}」` : '新建功能卡片'}
      subtitle="代理访问：本地无需可达，由跳板机转发；直连访问：浏览器直接打开目标地址。一张卡片还可以挂多条环境地址。"
      onClose={onClose}
      width={700}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="submit" form="card-form" className="btn btn-primary" disabled={busy || uploading}>
            {busy ? <span className="spinner" /> : null}
            {busy ? '保存中…' : '保存'}
          </button>
        </>
      }
    >
      <form id="card-form" onSubmit={submit}>
        {fieldError ? (
          <div className="alert alert-danger" style={{ marginBottom: 16 }}>
            {fieldError}
          </div>
        ) : null}

        {/* ---------- 实时预览 ---------- */}
        <div className="appearance-block" style={{ marginBottom: 18 }}>
          <div className="appearance-title">
            <ImageIcon size={15} />
            卡片预览
          </div>
          <div style={{ display: 'flex', justifyContent: 'center' }}>
            <div
              className="tile"
              data-bg={form.bg_style || undefined}
              style={{ width: 232, minHeight: 152, pointerEvents: 'none' }}
            >
              <div className="tile-open" style={{ cursor: 'default' }}>
                <CardIcon card={previewCard} size={46} lazy={false} />
                <span className="tile-body">
                  <span className="tile-title">{form.title || '功能名称'}</span>
                  <span className="tile-desc">{form.description || '这里显示功能说明。'}</span>
                </span>
                <span className="tile-foot">
                  {endpoints.length > 0 ? (
                    <span className="badge badge-env">{endpoints.length} 个环境</span>
                  ) : (
                    <span className={`badge ${form.open_mode === 'proxy' ? '' : 'badge-direct'}`}>
                      {form.open_mode === 'proxy' ? '代理访问' : '直连访问'}
                    </span>
                  )}
                </span>
              </div>
            </div>
          </div>
        </div>

        <div className="grid-2">
          <div className="field">
            <label htmlFor="f-title">功能名称 *</label>
            <input
              id="f-title"
              className="input"
              value={form.title}
              onChange={(event) => patch({ title: event.target.value })}
              placeholder="例如：字典录入"
              maxLength={128}
              required
            />
          </div>
          <div className="field">
            <label htmlFor="f-group">分组</label>
            <GroupPicker
              id="f-group"
              value={form.group_name ?? ''}
              knownGroups={knownGroups}
              onChange={(next) => patch({ group_name: next })}
              placeholder="常用工具"
            />
            <span className="hint">
              {knownGroups.length > 0
                ? '点下面的分组直接选用，也可以在输入框里写一个新分组。'
                : '还没有其他分组。直接输入即可新建，之后会自动出现在这里。'}
            </span>
          </div>
        </div>

        <div className="field">
          <label htmlFor="f-desc">功能说明</label>
          <textarea
            id="f-desc"
            className="textarea"
            value={form.description ?? ''}
            onChange={(event) => patch({ description: event.target.value })}
            placeholder="一句话说明这个工具解决什么问题，会显示在卡片上。"
            maxLength={512}
          />
        </div>

        <div className="field">
          <label htmlFor="f-url">目标地址 *</label>
          <input
            id="f-url"
            className="input mono"
            value={form.target_url}
            onChange={(event) => patch({ target_url: event.target.value })}
            placeholder="http://192.168.1.20:8080/"
            required
          />
          <span className="hint">
            填内网服务的完整地址。<strong>以 / 结尾</strong>表示这是一个目录，该目录下的相对路径一起代理；
            <strong>不以 / 结尾</strong>表示打开的是一个具体页面（如 Jenkins 的 <code>/login</code>），
            页面里的 <code>/static/**</code> 会按站点绝对路径解析。
          </span>
        </div>

        <div className="field">
          <label>访问方式</label>
          <div className="radio-group">
            {(
              [
                {
                  value: 'proxy' as OpenMode,
                  title: '代理访问（推荐）',
                  desc: '本地无需能访问内网，浏览器只连跳板机，由跳板机转发。',
                },
                {
                  value: 'direct' as OpenMode,
                  title: '直连访问',
                  desc: '浏览器直接打开目标地址，需要本地本身就能访问到（如已连 VPN）。',
                },
              ] satisfies Array<{ value: OpenMode; title: string; desc: string }>
            ).map((option) => (
              <label
                key={option.value}
                className={`radio-card${form.open_mode === option.value ? ' is-active' : ''}`}
              >
                <input
                  type="radio"
                  name="open_mode"
                  value={option.value}
                  checked={form.open_mode === option.value}
                  onChange={() => patch({ open_mode: option.value })}
                />
                <strong>{option.title}</strong>
                <span>{option.desc}</span>
              </label>
            ))}
          </div>
        </div>

        {/* ---------- 环境地址 ---------- */}
        <div className="appearance-block">
          <div className="appearance-title">
            <PlusIcon size={15} />
            环境地址（可选）
            {endpoints.length > 0 ? <span className="badge badge-muted">{endpoints.length} 条</span> : null}
          </div>

          <p className="hint" style={{ marginTop: 0, marginBottom: 10 }}>
            同一个系统有生产 / 测试 / 预发多套入口时，把它们的地址都挂在这张卡片上：
            点卡片会先弹出列表让你选一个，而不是直接跳转 —— 避免"看着像测试、其实是生产"。
            <strong>每条地址可以各自选择直连或代理</strong>，所以"生产走跳板机、内网测试直连"是成立的。
          </p>

          {endpoints.length === 0 ? (
            <p className="endpoint-empty">
              还没有环境地址。此时点卡片直接打开上面的「目标地址」——与只有一张地址时完全一样。
            </p>
          ) : (
            <div className="endpoint-editor">
              <div className="endpoint-editor-head">
                <span>名称</span>
                <span>地址</span>
                <span>打开方式</span>
                <span />
              </div>
              {endpoints.map((endpoint, index) => (
                <div className="endpoint-editor-row" key={endpoint.slug ?? `new-${index}`}>
                  <input
                    className="input"
                    value={endpoint.name}
                    onChange={(event) => patchEndpoint(index, { name: event.target.value })}
                    placeholder="生产 / 测试 / 预发"
                    maxLength={64}
                    aria-label={`第 ${index + 1} 条环境名称`}
                  />
                  <input
                    className="input mono"
                    value={endpoint.url}
                    onChange={(event) => patchEndpoint(index, { url: event.target.value })}
                    placeholder="http://10.0.0.9:8081/"
                    maxLength={1024}
                    aria-label={`第 ${index + 1} 条环境地址`}
                  />
                  <select
                    className="input"
                    value={endpoint.open_mode}
                    onChange={(event) =>
                      patchEndpoint(index, { open_mode: event.target.value as OpenMode })
                    }
                    aria-label={`第 ${index + 1} 条环境打开方式`}
                  >
                    <option value="proxy">代理</option>
                    <option value="direct">直连</option>
                  </select>
                  <div className="endpoint-editor-actions">
                    <button
                      type="button"
                      className="endpoint-btn"
                      onClick={() => moveEndpoint(index, -1)}
                      disabled={index === 0}
                      title="上移"
                      aria-label={`把 ${endpoint.name || `第 ${index + 1} 条`} 上移`}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className="endpoint-btn"
                      onClick={() => moveEndpoint(index, 1)}
                      disabled={index === endpoints.length - 1}
                      title="下移"
                      aria-label={`把 ${endpoint.name || `第 ${index + 1} 条`} 下移`}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      className="endpoint-btn is-danger"
                      onClick={() => removeEndpoint(index)}
                      title="删除这条地址"
                      aria-label={`删除 ${endpoint.name || `第 ${index + 1} 条`}`}
                    >
                      <TrashIcon size={13} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 10 }}>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={addEndpoint}
              disabled={endpoints.length >= MAX_ENDPOINTS}
            >
              <PlusIcon size={14} />
              添加一条地址
            </button>
            {endpoints.length > 0 ? (
              <span className="hint" style={{ marginTop: 0 }}>
                挂了环境地址后，上面的「目标地址 / 访问方式」在首页不再直接生效（保留作兜底，
                删除全部环境地址即恢复）。顺序就是弹窗里的显示顺序。
              </span>
            ) : null}
          </div>
        </div>

        {/* ---------- 图标与外观 ---------- */}
        <div className="appearance-block">
          <div className="appearance-title">
            <PaletteIcon size={15} />
            图标与外观
          </div>

          <div className="field" style={{ marginTop: 0 }}>
            <label>自定义图标图片</label>
            <div className="icon-uploader">
              <div className="icon-preview">
                {iconSrc ? <img src={iconSrc} alt="图标预览" /> : <span>无</span>}
              </div>
              <div className="icon-uploader-actions">
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  onClick={() => fileRef.current?.click()}
                  disabled={uploading}
                >
                  {uploading ? <span className="spinner spinner-dark" /> : <UploadIcon size={14} />}
                  {uploading ? '上传中…' : iconSrc ? '更换图片' : '上传图片'}
                </button>
                {iconSrc ? (
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost"
                    onClick={() => patch({ icon_url: '' })}
                    title="清除图片，改用下面的字符图标"
                  >
                    <TrashIcon size={14} />
                    清除
                  </button>
                ) : null}
                <input
                  ref={fileRef}
                  className="visually-hidden"
                  type="file"
                  accept={ACCEPT_ICON}
                  onChange={(event) => void handleIconFile(event.target.files?.[0])}
                />
              </div>
            </div>
            <span className="hint">
              PNG / JPG / WEBP / GIF / ICO / BMP，≤512KB。不设图片时使用下面的字符图标；SVG 因存在脚本风险不予支持。
            </span>
          </div>

          <div className="grid-2">
            <div className="field">
              <label htmlFor="f-icon">字符图标</label>
              <input
                id="f-icon"
                className="input"
                value={form.icon ?? ''}
                onChange={(event) => patch({ icon: event.target.value })}
                placeholder="如 U、#、SQL、文"
                maxLength={16}
                disabled={Boolean(iconSrc)}
              />
              <span className="hint">{iconSrc ? '已使用图片图标，字符图标暂不生效。' : '留空则取名称首字。'}</span>
            </div>
            <div className="field">
              <label>字符图标配色</label>
              <div className="swatch-row">
                {ICON_STYLE_OPTIONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    className={`swatch${(form.icon_style ?? 'blue') === option.value ? ' is-active' : ''}`}
                    onClick={() => patch({ icon_style: option.value })}
                    title={option.label}
                    aria-label={`图标配色 ${option.label}`}
                    disabled={Boolean(iconSrc)}
                  >
                    <span className="swatch-iconstyle" data-style={option.value}>
                      A
                    </span>
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="field">
            <label>卡片底色</label>
            <div className="swatch-row">
              {BG_STYLE_OPTIONS.map((option) => (
                <button
                  key={option.value || 'default-empty'}
                  type="button"
                  className={`swatch${(form.bg_style ?? '') === option.value ? ' is-active' : ''}`}
                  onClick={() => patch({ bg_style: option.value })}
                  title={option.label}
                  aria-label={`卡片底色 ${option.label}`}
                >
                  <span className="swatch-bg" data-bg={option.value || 'default'} />
                </button>
              ))}
            </div>
          </div>

          <div className="field">
            <label>图标主色（可选）</label>
            <div className="swatch-row">
              <button
                type="button"
                className={`swatch${!form.accent_color ? ' is-active' : ''}`}
                onClick={() => patch({ accent_color: '' })}
                title="跟随底色预设"
                aria-label="跟随底色预设"
              >
                <span className="swatch-color is-empty" />
              </button>
              {ACCENT_PRESETS.map((color) => (
                <button
                  key={color}
                  type="button"
                  className={`swatch${(form.accent_color ?? '').toLowerCase() === color ? ' is-active' : ''}`}
                  onClick={() => patch({ accent_color: color })}
                  title={color}
                  aria-label={`图标主色 ${color}`}
                >
                  <span className="swatch-color" style={{ background: color }} />
                </button>
              ))}
              <label className="color-picker" title="自定义颜色">
                <input
                  type="color"
                  value={isValidAccent(form.accent_color ?? '') ? (form.accent_color as string) : '#2b6cf6'}
                  onChange={(event) => patch({ accent_color: event.target.value })}
                  aria-label="自定义图标主色"
                />
                <span className="mono">{form.accent_color || '自定义'}</span>
              </label>
            </div>
            <span className="hint">
              主色会覆盖上面的字符图标配色；图片图标则用主色描边。留空表示跟随底色预设。
            </span>
          </div>
        </div>

        <div className="grid-2" style={{ marginTop: 16 }}>
          <div className="field">
            <label htmlFor="f-sort">排序值</label>
            <input
              id="f-sort"
              className="input"
              type="number"
              value={form.sort_order ?? 0}
              onChange={(event) => patch({ sort_order: Number(event.target.value) || 0 })}
            />
            <span className="hint">数字越小越靠前。也可以回首页用「调整顺序」拖拽排序。</span>
          </div>
          <div className="field">
            <label htmlFor="f-slug">标识（可选）</label>
            <input
              id="f-slug"
              className="input mono"
              value={slug}
              onChange={(event) => setSlug(event.target.value)}
              placeholder="dict-entry"
              disabled={isEdit}
              maxLength={64}
            />
            <span className="hint">
              {isEdit ? '标识创建后不可更改，保证已分享的链接不失效。' : '决定代理路径 /gw/标识/，留空自动生成。'}
            </span>
          </div>
        </div>

        <div style={{ display: 'flex', gap: 22, flexWrap: 'wrap', marginTop: 4 }}>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.enabled ?? true}
              onChange={(event) => patch({ enabled: event.target.checked })}
            />
            启用该卡片
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={form.open_in_new_tab ?? true}
              onChange={(event) => patch({ open_in_new_tab: event.target.checked })}
            />
            在新标签页打开
          </label>
          <label className="checkbox-row" title="内网自签证书场景建议保持关闭">
            <input
              type="checkbox"
              checked={form.verify_tls === true}
              onChange={(event) => patch({ verify_tls: event.target.checked ? true : null })}
            />
            校验目标 HTTPS 证书
          </label>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            style={{ marginLeft: 'auto' }}
            onClick={() => {
              patch({
                icon_url: '',
                bg_style: '',
                accent_color: '',
                icon_style: 'blue',
              })
            }}
            title="清空图标图片、底色与主色，恢复默认外观"
          >
            <RestoreIcon size={14} />
            恢复默认外观
          </button>
        </div>
      </form>
    </Modal>
  )
}
