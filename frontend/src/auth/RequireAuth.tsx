/**
 * T-44：路由层守卫。
 *
 * 修复前：守卫是**渲染期的一串 `&&`**（`{showAdminPanel && userRole === 'admin' && …}`），
 * 于是"能不能进"和"页面长什么样"揉在同一个 JSX 里 ——
 * 既无法直接输 URL 进入，也无法在组件内部安全地假设"我拿到的一定是有效令牌"。
 *
 * 现在：权限判断只发生在**路由出口**，被放行的子树拿到的 `token` 一定非空。
 *
 * 三个守卫的分工（刻意拆开，便于分别断言）：
 *   · `RequireAuth`    —— 未登录 → `/`（带 `redirect` 记忆，登录后可回跳）
 *   · `RequireAdmin`   —— 已登录但非管理员 → `/`（**不**跳登录页：他不是"没登录"）
 *   · `RequireGuest`   —— 已登录却访问 `/admin` 之外的管理入口时按需使用（保留口子）
 *
 * 注意：这是**前端体验层**的守卫，不是安全边界 ——
 * 真正的鉴权在后端 `require_admin`，前端守卫只负责"别把用户带进一个必然 401 的页面"。
 */

import type { ReactNode } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from './AuthContext';

/** 未登录时重定向到首页，并把来路记在 `state.from` 上。 */
export function RequireAuth({ children }: { children?: ReactNode }) {
  const { isAuthenticated } = useAuth();
  const location = useLocation();

  if (!isAuthenticated) {
    return <Navigate to="/" replace state={{ from: location.pathname + location.search }} />;
  }
  return <>{children ?? <Outlet />}</>;
}

/** 必须是管理员；已登录但角色不符时回首页，**不**回落登录页。 */
export function RequireAdmin({ children }: { children?: ReactNode }) {
  const { isAuthenticated, isAdmin } = useAuth();
  const location = useLocation();

  if (!isAuthenticated) {
    return <Navigate to="/" replace state={{ from: location.pathname + location.search }} />;
  }
  if (!isAdmin) {
    return <Navigate to="/" replace />;
  }
  return <>{children ?? <Outlet />}</>;
}

/** 已登录用户不应再停留在登录入口（目前登录是弹窗，保留给后续 `/login` 路由化）。 */
export function RequireGuest({ children }: { children?: ReactNode }) {
  const { isAuthenticated } = useAuth();
  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }
  return <>{children ?? <Outlet />}</>;
}
