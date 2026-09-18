/** 登录页。 */

import { useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ApiError } from '../lib/api'
import { useAuth } from '../lib/auth'

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setError('')
    setBusy(true)
    try {
      await login(username.trim(), password)
      navigate('/', { replace: true })
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : '登录失败，请检查网络后重试',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page login-shell">
      <form className="login-card" onSubmit={onSubmit}>
        <div className="login-mark">工</div>
        <h1>开发工具箱</h1>
        <p>集中管理常用研发工具，点击卡片进入功能页面。</p>

        {error ? (
          <div className="alert alert-danger" style={{ marginBottom: 16 }}>
            {error}
          </div>
        ) : null}

        <div className="field">
          <label htmlFor="username">用户名</label>
          <input
            id="username"
            className="input"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
          />
        </div>

        <div className="field">
          <label htmlFor="password">密码</label>
          <input
            id="password"
            className="input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </div>

        <button className="btn btn-primary" type="submit" disabled={busy} style={{ width: '100%', marginTop: 6 }}>
          {busy ? <span className="spinner" /> : null}
          {busy ? '登录中…' : '登录'}
        </button>

        <p className="muted" style={{ fontSize: 12, marginTop: 18, marginBottom: 0, lineHeight: 1.7 }}>
          首次部署时，管理员初始密码会在服务端启动日志中打印一次。
        </p>
      </form>
    </div>
  )
}
