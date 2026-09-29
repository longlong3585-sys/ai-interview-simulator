/**
 * T-44 / Bug 2（前端）：认证状态的**唯一真源** + 持久化。
 *
 * 修复前：`token` / `userRole` / `username` 是 `App.tsx` 里的三个 `useState`，
 * 初值直接 `localStorage.getItem(...)`，写入散落在登录回调、登出回调里。
 * 后果：
 *   ① 任何组件想读 token 只能靠 `props` 一层层传（`AdminPanelContent({ token })`）；
 *   ② 路由层的守卫无从下手 —— 没有"当前是否已认证"的公共读口；
 *   ③ 登出 / 令牌失效时，散落的副本（含子组件的闭包）各自为政。
 *
 * 现在：`AuthProvider` 持有状态，**任何状态变更都同步落 localStorage**，
 * 所以"刷新后 userId/role 仍在"不再是靠各调用点自觉，而是**由唯一写路径保证**。
 *
 * 边界（刻意）：本文件**不**做令牌校验请求，也**不**注册全局 401 处理 ——
 * 那是 T-36（api 层收敛）/ T-50（滑动续期）的范围。这里只解决"状态与持久化"。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

export type UserRole = 'admin' | 'user' | null;

export const AUTH_STORAGE_KEYS = ['token', 'role', 'username'] as const;
export type AuthStorageKey = (typeof AUTH_STORAGE_KEYS)[number];

export interface AuthSnapshot {
  /** localStorage 里是否有令牌（无需校验即认为"看起来已登录"）。 */
  token: string | null;
  /** 服务端返回的用户主键；只在登录响应里出现，刷新后需要重新拉取（T-41）。 */
  userId: number | null;
  role: UserRole;
  username: string;
}

export interface SignInPayload {
  token: string;
  userId?: number | null;
  role?: UserRole;
  username?: string;
}

export interface AuthContextValue extends AuthSnapshot {
  /** 是否持有一个非空令牌。 */
  isAuthenticated: boolean;
  isAdmin: boolean;
  signIn: (payload: SignInPayload) => void;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function readStored(key: AuthStorageKey): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    // 隐私模式 / 沙箱里 localStorage 可能直接抛错 —— 认证降级为"仅内存"，
    // 不能让读取失败把整个应用打成白屏。
    return null;
  }
}

function writeStored(key: AuthStorageKey, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* 同上：写不进去也不影响本次会话的可用性 */
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => readStored('token'));
  const [role, setRole] = useState<UserRole>(() => (readStored('role') as UserRole) ?? null);
  const [username, setUsername] = useState<string>(() => readStored('username') ?? '');
  // userId 不落 localStorage（T-40 的口径）：它是服务端会话事实，
  // 刷新后由 /api/user/profile 重新确认，避免用一个可能过期的 id 去拼请求。
  const [userId, setUserId] = useState<number | null>(null);

  useEffect(() => {
    writeStored('token', token);
  }, [token]);

  useEffect(() => {
    writeStored('role', role);
  }, [role]);

  useEffect(() => {
    writeStored('username', username || null);
  }, [username]);

  const signIn = useCallback((payload: SignInPayload) => {
    setToken(payload.token);
    setRole(payload.role ?? null);
    setUsername(payload.username ?? '');
    setUserId(payload.userId ?? null);
  }, []);

  const signOut = useCallback(() => {
    setToken(null);
    setRole(null);
    setUsername('');
    setUserId(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      token,
      userId,
      role,
      username,
      isAuthenticated: Boolean(token),
      isAdmin: role === 'admin',
      signIn,
      signOut,
    }),
    [token, userId, role, username, signIn, signOut]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    // 忘记包 Provider 是接线错误，宁可当场响亮地失败，也不要静默退化成"永远未登录"。
    throw new Error('useAuth() 必须在 <AuthProvider> 内部使用（见 src/main.tsx）');
  }
  return ctx;
}

export { AuthContext };
