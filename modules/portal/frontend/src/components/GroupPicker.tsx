/**
 * 分组选择器：既能下拉选择已有分组，也能直接输入新分组。
 *
 * 为什么不用 `<input list>` + `<datalist>`
 * --------------------------------------
 * 原生 datalist 的候选列表由浏览器与输入法联合决定：中文输入法下候选经常不弹出，
 * macOS 上 `list` 属性的表现也不一致，结果就是"只能手动输入或选默认分组"。
 * 这里改成受控的自绘下拉：输入框仍是自由文本，但候选完全由我们控制，
 * 并且把**全部已有分组**以 chip 形式直接铺在输入框下方 —— 一次点击即选，
 * 不依赖任何下拉行为是否被浏览器正确实现。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ChevronDownIcon, PlusIcon } from './Icons'

/** 最近的可滚动祖先（弹窗正文就是它）；用于判断下拉该朝哪边弹。 */
function scrollParent(el: HTMLElement | null): HTMLElement | null {
  let node = el?.parentElement ?? null
  while (node) {
    const overflowY = getComputedStyle(node).overflowY
    if (overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'hidden') return node
    node = node.parentElement
  }
  return null
}

/** 下拉列表最大高度（与 CSS 里的 max-height 保持一致） */
const LIST_MAX_HEIGHT = 216

interface GroupPickerProps {
  id: string
  value: string
  knownGroups: string[]
  onChange: (value: string) => void
  placeholder?: string
  maxLength?: number
}

export function GroupPicker({
  id,
  value,
  knownGroups,
  onChange,
  placeholder,
  maxLength = 64,
}: GroupPickerProps) {
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const [dropUp, setDropUp] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const anchorRef = useRef<HTMLDivElement | null>(null)
  const composingRef = useRef(false)

  const typed = value.trim()

  // 输入内容作为过滤词；没输入时列出全部已有分组
  const filtered = useMemo(() => {
    if (!typed) return knownGroups
    const needle = typed.toLowerCase()
    return knownGroups.filter((group) => group.toLowerCase().includes(needle))
  }, [knownGroups, typed])

  // 输入的内容不是任何已有分组时，末尾提供一项"新建分组"
  const canCreate =
    Boolean(typed) && !knownGroups.some((group) => group.toLowerCase() === typed.toLowerCase())
  const optionCount = filtered.length + (canCreate ? 1 : 0)

  useEffect(() => {
    if (!open) return

    // 弹窗正文是滚动容器，会裁掉溢出的绝对定位元素。
    // 下方放不下整张列表时改成向上弹出，否则用户看不到候选。
    const anchor = anchorRef.current
    const container = scrollParent(anchor)
    if (anchor) {
      const rect = anchor.getBoundingClientRect()
      const box = container ? container.getBoundingClientRect() : { top: 0, bottom: window.innerHeight }
      const below = box.bottom - rect.bottom
      const above = rect.top - box.top
      setDropUp(below < LIST_MAX_HEIGHT + 24 && above > below)
    }

    const onDocumentMouseDown = (event: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocumentMouseDown)
    return () => document.removeEventListener('mousedown', onDocumentMouseDown)
  }, [open])

  // 候选数量变少时把高亮项收回到有效范围内
  useEffect(() => {
    setActive((current) => (current >= optionCount ? optionCount - 1 : current))
  }, [optionCount])

  function commit(next: string) {
    onChange(next)
    setOpen(false)
    setActive(-1)
  }

  function handleKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    // 中文输入法组合期间，方向键/回车属于候选词操作，不能拿来选分组
    if (composingRef.current || event.nativeEvent.isComposing) return

    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!open) {
        setOpen(true)
        setActive(0)
        return
      }
      if (optionCount === 0) return
      const delta = event.key === 'ArrowDown' ? 1 : -1
      setActive((current) => {
        const next = current + delta
        if (next < 0) return optionCount - 1
        if (next >= optionCount) return 0
        return next
      })
      return
    }

    if (event.key === 'Enter') {
      // 没有高亮项时把回车让给表单（保存卡片），避免"想选分组却存了卡片"
      if (!open || active < 0) return
      event.preventDefault()
      commit(active < filtered.length ? filtered[active] : typed)
      return
    }

    if (event.key === 'Escape') {
      if (!open) return // 下拉没开时，Escape 继续用来关闭弹层
      event.preventDefault()
      event.stopPropagation() // 别让弹层把整个编辑弹窗一起关掉
      setOpen(false)
      setActive(-1)
    }
  }

  return (
    <div className="combo" ref={wrapRef}>
      {/* 列表锚在输入框这一层，而不是整个 .combo（否则会被下面的 chip 行推远） */}
      <div className="combo-anchor" ref={anchorRef}>
        <div className="combo-control">
          <input
            id={id}
            className="input combo-input"
            type="text"
            role="combobox"
            aria-expanded={open}
            aria-controls={`${id}-listbox`}
            aria-autocomplete="list"
            aria-activedescendant={open && active >= 0 ? `${id}-opt-${active}` : undefined}
            autoComplete="off"
            spellCheck={false}
            value={value}
            placeholder={placeholder}
            maxLength={maxLength}
            onChange={(event) => {
              onChange(event.target.value)
              setOpen(true)
              setActive(-1)
            }}
            onFocus={() => setOpen(true)}
            onKeyDown={handleKeyDown}
            onCompositionStart={() => {
              composingRef.current = true
            }}
            onCompositionEnd={(event) => {
              composingRef.current = false
              onChange(event.currentTarget.value)
            }}
          />
          <button
            type="button"
            className="combo-toggle"
            aria-label={open ? '收起分组列表' : '展开分组列表'}
            aria-expanded={open}
            onClick={() => setOpen((current) => !current)}
          >
            <ChevronDownIcon size={15} />
          </button>
        </div>

        {open ? (
          <ul
            className={`combo-list${dropUp ? ' is-up' : ''}`}
            id={`${id}-listbox`}
            role="listbox"
            aria-label="已有分组"
          >
            {filtered.map((group, index) => (
              <li
                key={group}
                id={`${id}-opt-${index}`}
                role="option"
                aria-selected={group === value}
                className={
                  `combo-item${index === active ? ' is-active' : ''}` +
                  `${group === value ? ' is-selected' : ''}`
                }
                onMouseEnter={() => setActive(index)}
                // 阻止默认行为，避免点选项时输入框先失焦导致下拉被关掉
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => commit(group)}
              >
                {group}
              </li>
            ))}

            {canCreate ? (
              <li
                id={`${id}-opt-${filtered.length}`}
                role="option"
                aria-selected={false}
                className={`combo-item combo-create${filtered.length === active ? ' is-active' : ''}`}
                onMouseEnter={() => setActive(filtered.length)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => commit(typed)}
              >
                <PlusIcon size={13} />
                新建分组「{typed}」
              </li>
            ) : null}

            {optionCount === 0 ? (
              <li className="combo-empty">暂无已有分组，直接输入即可新建</li>
            ) : null}
          </ul>
        ) : null}
      </div>

      {knownGroups.length > 0 ? (
        <div className="chip-row">
          <span className="chip-row-label">已有分组</span>
          {knownGroups.map((group) => (
            <button
              key={group}
              type="button"
              className={`chip${group === value ? ' is-active' : ''}`}
              aria-pressed={group === value}
              onClick={() => commit(group)}
            >
              {group}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
