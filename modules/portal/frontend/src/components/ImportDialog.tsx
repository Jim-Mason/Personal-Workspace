/**
 * 导入配置对话框。
 *
 * 打开时先做一次"预演"（dry_run），把会发生的增/改/删数量摆出来给用户确认；
 * 只有用户点了「确认导入」才真正写库。避免一次误操作把整站卡片覆盖掉。
 *
 * 解析全部在服务端完成：xlsx / csv 的读表、GBK 解码、中文取值、行号定位都属于
 * `card_sheet` 的职责，前端只负责把文件传上去、把结果显示清楚。这样"表格能填什么"
 * 与"接口接受什么"永远是同一份规则。
 */

import { useEffect, useState } from 'react'
import { ApiError, api } from '../lib/api'
import type { CardImportResult, ImportMode } from '../lib/types'
import { AlertIcon } from './Icons'
import { Modal } from './Modal'

interface ImportDialogProps {
  file: File
  /** 当前库里已有多少张卡片，用于提示 replace 的影响面 */
  existingCount: number
  onClose: () => void
  onDone: (message: string) => void | Promise<void>
  onError: (message: string) => void
}

const MODES: Array<{ value: ImportMode; title: string; desc: string }> = [
  {
    value: 'merge',
    title: '合并（推荐）',
    desc: '按标识匹配：已存在的卡片更新配置，不存在的新建。库里其他卡片不受影响。',
  },
  {
    value: 'replace',
    title: '覆盖全部',
    desc: '先清空当前所有卡片，再按导入内容重建。适合整机还原或换环境迁移。',
  },
]

/** 把文件名转成用户能认出的格式说明 */
function fileFormatLabel(name: string): string {
  const extension = name.toLowerCase().split('.').pop() ?? ''
  if (extension === 'xlsx' || extension === 'xlsm') return 'Excel 表格'
  if (extension === 'xls') return '旧版 Excel'
  if (extension === 'csv') return 'CSV 表格'
  if (extension === 'json') return 'JSON 配置'
  return '配置文件'
}

export function ImportDialog({
  file,
  existingCount,
  onClose,
  onDone,
  onError,
}: ImportDialogProps) {
  const [mode, setMode] = useState<ImportMode>('merge')
  const [preview, setPreview] = useState<CardImportResult | null>(null)
  const [previewing, setPreviewing] = useState(true)
  const [previewError, setPreviewError] = useState('')
  const [importing, setImporting] = useState(false)

  useEffect(() => {
    let cancelled = false
    setPreviewing(true)
    setPreview(null)
    setPreviewError('')
    ;(async () => {
      try {
        const result = await api.importCardsFile(file, mode, true)
        if (!cancelled) setPreview(result)
      } catch (err) {
        // 文件读不了（格式不对、编码不对）时把原因留在对话框里，
        // 而不是只弹一个转瞬即逝的 toast —— 用户需要照着提示去改文件
        const message = err instanceof ApiError ? err.message : '预演失败'
        if (!cancelled) {
          setPreviewError(message)
          onError(message)
        }
      } finally {
        if (!cancelled) setPreviewing(false)
      }
    })()
    return () => {
      cancelled = true
    }
    // onError 每次渲染都是新函数，故意不入依赖，避免重复预演
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, mode])

  async function confirm() {
    setImporting(true)
    try {
      const result = await api.importCardsFile(file, mode, false)
      await onDone(
        `导入完成：新建 ${result.created}、更新 ${result.updated}、删除 ${result.deleted}、跳过 ${result.skipped}`,
      )
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '导入失败')
    } finally {
      setImporting(false)
    }
  }

  const subtitle = previewing
    ? `${file.name} · 正在解析…`
    : preview
      ? `${file.name} · 表格里 ${preview.total} 行数据`
      : `${file.name} · 解析失败`

  return (
    <Modal
      title="导入卡片配置"
      subtitle={subtitle}
      onClose={onClose}
      width={640}
      footer={
        <>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={importing}>
            取消
          </button>
          <button
            type="button"
            className={`btn ${mode === 'replace' ? 'btn-danger' : 'btn-primary'}`}
            onClick={() => void confirm()}
            disabled={importing || previewing || Boolean(previewError)}
          >
            {importing ? <span className="spinner" /> : null}
            {importing ? '导入中…' : mode === 'replace' ? '确认覆盖导入' : '确认导入'}
          </button>
        </>
      }
    >
      <div className="field" style={{ marginTop: 0 }}>
        <label>导入方式</label>
        <div className="radio-group">
          {MODES.map((option) => (
            <label
              key={option.value}
              className={`radio-card${mode === option.value ? ' is-active' : ''}`}
            >
              <input
                type="radio"
                name="import_mode"
                value={option.value}
                checked={mode === option.value}
                onChange={() => setMode(option.value)}
              />
              <strong>{option.title}</strong>
              <span>{option.desc}</span>
            </label>
          ))}
        </div>
      </div>

      {mode === 'replace' ? (
        <div className="alert alert-warning" style={{ marginBottom: 16 }}>
          <AlertIcon size={16} />
          <div>
            覆盖导入会先删除当前全部 <strong>{existingCount}</strong> 张卡片。若只是想补充或更新，
            请改选「合并」。
          </div>
        </div>
      ) : null}

      {previewError ? (
        <div className="alert alert-danger" style={{ marginBottom: 16 }}>
          <AlertIcon size={16} />
          <div>{previewError}</div>
        </div>
      ) : null}

      <div style={{ fontSize: 13, fontWeight: 650, color: 'var(--ink-700)', marginBottom: 8 }}>
        影响预览{previewing ? '（计算中…）' : ''}
      </div>

      <div className="import-stats">
        <div className="import-stat is-created">
          <b>{previewing ? '—' : (preview?.created ?? 0)}</b>
          <span>新建</span>
        </div>
        <div className="import-stat is-updated">
          <b>{previewing ? '—' : (preview?.updated ?? 0)}</b>
          <span>更新</span>
        </div>
        <div className="import-stat is-deleted">
          <b>{previewing ? '—' : (preview?.deleted ?? 0)}</b>
          <span>删除</span>
        </div>
        <div className="import-stat is-skipped">
          <b>{previewing ? '—' : (preview?.skipped ?? 0)}</b>
          <span>跳过</span>
        </div>
      </div>

      {preview && preview.errors.length > 0 ? (
        <>
          <div style={{ fontSize: 13, fontWeight: 650, color: 'var(--ink-700)', marginBottom: 8 }}>
            以下条目会被跳过
          </div>
          <ul className="import-errors">
            {preview.errors.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </>
      ) : null}

      <div className="import-file" style={{ marginTop: 16 }}>
        <span className="badge badge-muted">{fileFormatLabel(file.name)}</span>
        <div>
          <strong>{file.name}</strong>
          <span className="muted">
            表格里留空的格子按默认值处理；预演不会写入数据库，只有点「确认导入」才会生效。
          </span>
        </div>
      </div>
    </Modal>
  )
}
