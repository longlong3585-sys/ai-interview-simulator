import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import './index.css'
import App from './App.tsx'
import AdminPage from './admin/AdminPage.tsx'
import { AuthBridge } from './auth/AuthBridge.tsx'
import { AuthProvider } from './auth/AuthContext.tsx'
import { RequireAuth } from './auth/RequireAuth.tsx'

/**
 * T-44：应用入口改为**路由装配**。
 *
 * 修复前：`main.tsx` 直接 `<App />`，`react-router-dom` 虽然装在 package.json 里
 * 却从未被 import —— 于是"页面"只能靠 `App.tsx` 内部的一堆布尔量
 * （`showAdminPanel` / `showProfile` / `report` …）来切换：
 *   · 无法直接输 URL 进入任何页面（刷新即回首页）；
 *   · 守卫只能写成渲染期的 `&&`；
 *   · 想复用某个页面，得把它从 2500 行的文件里抠出来。
 *
 * 现在：`AuthProvider`（认证状态唯一真源）+ `BrowserRouter` 包在最外层，
 * 每个页面是独立路由；守卫放在**路由层**（`RequireAuth` / `RequireAdmin`）。
 *
 * 说明：`/admin` 已按路由拆分（T-45）；面谈主流程仍由 `<App />` 承载，
 * 其内部视图的路由化留给 T-46（`InterviewRoom`）、T-47（报告/资料/通知/题库）。
 * `*` 兜底回首页，避免手输错误路径得到白屏。
 */
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthProvider>
      {/* T-36：把 AuthContext 的登出接进统一 HTTP 出口（401 → 统一登出 + 续期头集中处理）。 */}
      <AuthBridge />
      <BrowserRouter>
        <Routes>
          <Route path="/admin" element={<AdminPage />} />
          <Route
            path="/"
            element={
              <RequireAuth>
                <App />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  </StrictMode>,
)
