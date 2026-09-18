/**
 * 导出格式选择：一个按钮 + 下拉菜单。
 *
 * 为什么不是三个并列按钮
 * ----------------------
 * "导出成 Excel 还是 CSV" 对使用者不是自明的选择，三个并列按钮会把工具栏挤乱，
 * 还得靠 tooltip 解释区别。改成下拉后，每一项都能写清"适合拿来干什么"。
 *
 * 交互与 GroupPicker 保持一致：点外部关闭、Esc 关闭、菜单项用 mousedown 阻止
 * 默认行为，避免点选项时按钮先失焦导致菜单被关掉。
 */

import { useEffect, useRef, useState } from 'react'
import type { ExportFormat } from '../lib/types'
import { ChevronDownIcon, DownloadIcon } from './Icons'

interface ExportMenuProps {
  busy: boolean
  onPick: (format: ExportFormat) => void
}

const OPTIONS: Array<{ value: ExportFormat; title: string; desc: string }> = [
  {
    value: 'xlsx',
    title: 'Excel 表格（.xlsx）',
    desc: '带字段说明页与下拉选项，适合在 Excel 里成批修改后导回',
  },
  {
    value: 'csv',
    title: 'CSV 表格（.csv）',
    desc: '纯文本表格，记事本、命令行、脚本都能直接读',
  },
  {
    value: 'json',
    title: 'JSON（.json）',
    desc: '原始格式，字段最全，供脚本处理与换机备份',
  },
]

export function ExportMenu({ busy, onPick }: ExportMenuProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return

    const onDocumentMouseDown = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false)
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('mousedown', onDocumentMouseDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onDocumentMouseDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="menu" ref={wrapRef}>
      <button
        type="button"
        className="btn btn-ghost"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={busy}
        onClick={() => setOpen((current) => !current)}
      >
        {busy ? <span className="spinner spinner-dark" /> : <DownloadIcon size={15} />}
        导出配置
        <ChevronDownIcon size={14} />
      </button>

      {open ? (
        <div className="menu-list" role="menu" aria-label="选择导出格式">
          {OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              role="menuitem"
              className="menu-item"
              // 阻止默认行为：否则点击时按钮先失焦，菜单会在 onClick 之前被关掉
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                setOpen(false)
                onPick(option.value)
              }}
            >
              <strong>{option.title}</strong>
              <span>{option.desc}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
