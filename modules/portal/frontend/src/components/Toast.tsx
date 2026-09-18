/** 轻提示：把后端错误/成功信息以非阻塞方式呈现。 */

import { createContext, useCallback, useContext, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { AlertIcon, CheckIcon } from './Icons'

type ToastKind = 'info' | 'success' | 'error'

interface ToastItem {
  id: number
  kind: ToastKind
  text: string
}

interface ToastApi {
  info: (text: string) => void
  success: (text: string) => void
  error: (text: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)

  const push = useCallback((kind: ToastKind, text: string) => {
    const id = nextId.current++
    setItems((current) => [...current, { id, kind, text }])
    window.setTimeout(() => {
      setItems((current) => current.filter((item) => item.id !== id))
    }, kind === 'error' ? 5200 : 2800)
  }, [])

  const api = useMemo<ToastApi>(
    () => ({
      info: (text) => push('info', text),
      success: (text) => push('success', text),
      error: (text) => push('error', text),
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {items.map((item) => (
          <div key={item.id} className={`toast toast-${item.kind}`}>
            {item.kind === 'success' ? <CheckIcon size={15} /> : <AlertIcon size={15} />}
            <span>{item.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast 必须在 ToastProvider 内部使用')
  return context
}
