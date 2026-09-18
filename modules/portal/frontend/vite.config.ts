import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发态把 /api 与 /gw 代理到后端；生产态前端由后端同源托管，不走这里。
// 后端不在默认端口时，改下面这一行即可。
const BACKEND = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/gw': { target: BACKEND, changeOrigin: true, ws: true },
      '/health': { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
})
