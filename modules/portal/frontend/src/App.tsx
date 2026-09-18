/** 路由与应用外壳。 */

import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import type { ReactElement } from 'react'
import { useAuth } from './lib/auth'
import { AdminPage } from './pages/AdminPage'
import { LoginPage } from './pages/LoginPage'
import { ToolboxPage } from './pages/ToolboxPage'

function FullPageSpinner({ text }: { text: string }) {
  return (
    <div className="page">
      <div className="center-block">
        <span className="spinner spinner-dark" />
        {text}
      </div>
    </div>
  )
}

/** 需要登录；未登录时跳登录页并记住来源路径。 */
function RequireAuth({ children, adminOnly = false }: { children: ReactElement; adminOnly?: boolean }) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) return <FullPageSpinner text="正在校验登录状态…" />

  if (!user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }

  if (adminOnly && user.role !== 'admin') {
    return (
      <div className="page">
        <div className="container">
          <div className="alert alert-warning">
            该页面仅管理员可访问，请使用管理员账号登录。
          </div>
        </div>
      </div>
    )
  }

  return children
}

export default function App() {
  const { user, loading } = useAuth()

  return (
    <Routes>
      <Route
        path="/login"
        element={loading ? <FullPageSpinner text="加载中…" /> : user ? <Navigate to="/" replace /> : <LoginPage />}
      />
      <Route
        path="/"
        element={
          <RequireAuth>
            <ToolboxPage />
          </RequireAuth>
        }
      />
      <Route
        path="/admin"
        element={
          <RequireAuth adminOnly>
            <AdminPage />
          </RequireAuth>
        }
      />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
