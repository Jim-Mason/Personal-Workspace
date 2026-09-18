import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ToastProvider } from './components/Toast'
import { AuthProvider } from './lib/auth'
import './styles/global.css'

/**
 * 计算前端路由的基路径。
 *
 * 不这么做的话，被挂到子路径下就废了：浏览器地址是 /portal/，而
 * react-router 会拿 `/portal/` 去匹配 `/`、`/admin` 这些路由 —— 一个都匹配
 * 不上，页面直接空掉。它需要的是 basename，自己把前缀摘掉再匹配。
 *
 * 取值的优先级：
 *   1. window.__LOCALDECK__.mount —— 中台反向代理注入的挂载点
 *   2. import.meta.env.BASE_URL —— 构建时由 vite 的 base 指定
 *   3. '/' —— 独立部署在根路径
 *
 * 留这三层兜底，是为了让**同一份构建产物**既能被中台托管、也能自己单独跑
 * （deploy/start.sh 那条路径），不必为两种场景各构建一次。
 */
function resolveBasename(): string {
  const injected = (window as unknown as { __LOCALDECK__?: { mount?: string } }).__LOCALDECK__
  if (injected?.mount) return injected.mount

  const base = import.meta.env.BASE_URL
  return base && base !== '/' ? base.replace(/\/+$/, '') : '/'
}

const container = document.getElementById('root')
if (!container) throw new Error('找不到 #root 挂载点')

createRoot(container).render(
  <StrictMode>
    <BrowserRouter basename={resolveBasename()}>
      <ToastProvider>
        <AuthProvider>
          <App />
        </AuthProvider>
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
)
