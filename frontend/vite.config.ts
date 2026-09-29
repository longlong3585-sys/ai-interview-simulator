import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * T-34 / Bug 4（P0）：开发与预览期的**同源代理**。
 *
 * 前端现在默认用相对地址（`API_BASE_URL = ''`），所以 `npm run dev` 时
 * `fetch('/api/chat')` 会打到 **Vite 自己**（默认 5173）—— 必须由这里转发到后端，
 * 否则整个开发环境都点不动。同理 `npm run preview`（预览）也要转发，
 * 否则"预览环境下所有按钮可点"这条验收无从谈起。
 *
 * 代理目标**不进前端产物**：`vite.config.ts` 是 Node 侧的构建配置，
 * 永远不会被打包进浏览器代码；变量名也刻意**不带 `VITE_` 前缀**（那种才会被内联）。
 * 优先级：shell 环境变量 > `.env*` 文件 > 内置默认值。
 *
 * 换后端地址（例如连测试环境的 10.0.0.5）：
 *   PowerShell:  $env:DEV_PROXY_TARGET='http://10.0.0.5:8000'; npm run dev
 *   或在 frontend/.env.local 里写 DEV_PROXY_TARGET=http://10.0.0.5:8000
 */
export default defineConfig(({ mode }) => {
  // 第三个参数传空串 = 连**非** VITE_ 前缀的变量一起读（loadEnv 默认只读 VITE_）
  const fileEnv = loadEnv(mode, process.cwd(), '')
  const env = { ...fileEnv, ...process.env }
  const target = env.DEV_PROXY_TARGET || 'http://127.0.0.1:8000'

  const proxy = {
    // 业务接口
    '/api': { target, changeOrigin: true },
    // 头像等静态上传物（ProfilePanel 用 `${API_BASE_URL}${avatar}` 直接当 img src）
    '/uploads': { target, changeOrigin: true },
  }

  return {
    plugins: [react()],
    server: { proxy },
    preview: { proxy },
  }
})
