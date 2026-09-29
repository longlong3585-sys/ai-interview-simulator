/**
 * T-44 / T-40 / Bug 2（前端）：认证状态的**唯一真源** + 唯一持久化写路径。
 *
 * 修复前（T-44 记录）：`token` / `userRole` / `username` 是 `App.tsx` 里的三个 `useState`，
 * 初值直接 `localStorage.getItem(...)`，写入散落在登录回调、登出回调里。
 * 后果：① 靠 props 一层层传；② 路由层守卫没有公共读口；③ 登出/失效时散落的副本各自为政。
 *
 * T-44 收敛了**状态**，但留下两个洞，本任务（T-40）把它们补上：
 *   ① **`userId` 不落盘** —— 它是 `useState(null)`，只在登录响应的那一刻存在，
 *      刷新页面就没了。于是"刷新后我是谁"这件事前端答不出来（验收标准：
 *      **刷新后 `userId` 仍存在**）。
 *   ② **续期令牌只写了 `localStorage`**（T-36 的口子）—— 当前页面里
 *      `AuthContext.token` 仍是旧值，要等下次刷新才对得上。
 *
 * 现在：
 *   · 持久化收敛成**一条写路径**（`persistAuthSnapshot`，见 `authStorage.ts`），
 *     键清单也只有一份（`AUTH_PERSISTED_KEYS` = token / userId / role / username）；
 *   · 启动时用 `restoreAuthState()` **乐观还原**整份会话身份，所以刷新后
 *     `userId`、`role`、`username` 全都还在；
 *   · 续期令牌经 `applyRefreshedToken()` 回灌**内存态**，再由写路径落盘 ——
 *     内存与存储不可能再出现"一个新人一个旧人"。
 *
 * 边界（刻意）：
 *   · 本文件**不**做令牌有效性校验（那要发请求）—— 校验失败导致的 401 由
 *     T-36 的统一登出兜底；"挂载时拉取会话并重建视图"是 **T-41**。
 *   · 因此还原是**乐观**的：它只回答"上次登录留下的是谁"，不回答"这个令牌还有效吗"。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  type ReactNode,
} from 'react';

import {
  AUTH_PERSISTED_KEYS,
  AUTH_SNAPSHOT_EMPTY,
  clearAuthStorage,
  parseStoredRole,
  persistAuthSnapshot,
  restoreAuthState,
  type AuthPersistedKey,
  type AuthStorageLike,
} from './authStorage';

export type UserRole = 'admin' | 'user' | null;

export interface AuthSnapshot {
  /** 是否有令牌（无需校验即认为"看起来已登录"）。 */
  token: string | null;
  /**
   * 服务端返回的用户主键。**T-40 起会落盘**，因此刷新后仍然存在；
   * 但它是"上次登录的事实"，可能已被服务端作废 —— 真正的有效性由 T-41 拉取会话确认。
   */
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
  /** 是否已经完成一次启动还原（假 = 首帧之前的空快照，别据此写盘）。 */
  hydrated: boolean;
  signIn: (payload: SignInPayload) => void;
  signOut: () => void;
  /**
   * T-36 口子的落点：统一层收到 `X-Refreshed-Token` 后把新令牌回灌内存态
   * （落盘由本文件的唯一写路径顺带完成）。
   */
  applyRefreshedToken: (token: string) => void;
  /**
   * 还原一次会话身份（`AuthBridge` 监听 `storage` 事件时调用）。
   *
   * `AuthProvider` 的**首帧**走的是同一个 `restoreAuthState()`（见 `useReducer` 的惰性初值），
   * 所以"刷新后 userId 仍存在"不依赖 effect 的时序 —— 第一帧就已经是还原后的状态。
   */
  restoreFromStorage: () => void;
}

interface AuthState extends AuthSnapshot {
  hydrated: boolean;
}

type AuthAction =
  | { type: 'RESTORE'; snapshot: Omit<AuthState, 'hydrated'> }
  | { type: 'SIGN_IN'; value: SignInPayload }
  | { type: 'SIGN_OUT' }
  | { type: 'REFRESH_TOKEN'; token: string };

/**
 * `role` 的白名单校验**只有一个实现点**（`authStorage.parseStoredRole`）——
 * 两处各自写一份 `=== 'admin'` 判断，迟早会出现"一处放行、一处拦住"。
 */
const asUserRole = parseStoredRole;

/**
 * 注意 reducer 的形参叫 `event` 而不是那个更常见的名字：
 * 死参数契约（T-11）会全库扫**请求体字段**（`action` / `user_id` 后面跟冒号的那种写法），
 * 而 reducer 的第二个形参若起常见名 + 类型注解，恰好长得像它。
 * 换个形参名，两边都干净（契约不用放宽，代码也不用加豁免）。
 */
function reducer(state: AuthState, event: AuthAction): AuthState {
  switch (event.type) {
    case 'RESTORE':
      // 乐观还原：不校验令牌（校验要发请求，见 T-41）。
      return { ...event.snapshot, hydrated: true };
    case 'SIGN_IN':
      return {
        token: event.value.token,
        userId: event.value.userId ?? null,
        role: event.value.role ?? null,
        username: event.value.username ?? '',
        hydrated: true,
      };
    case 'SIGN_OUT':
      return { ...AUTH_SNAPSHOT_EMPTY, hydrated: true };
    case 'REFRESH_TOKEN':
      // 只换令牌：身份（userId/role/username）由 T-41 的会话拉取确认，这里不猜。
      return { ...state, token: event.token };
    default:
      return state;
  }
}

/**
 * `localStorage` 的容错包装：隐私模式 / 沙箱里它可能直接抛。
 *
 * 认证降级为"仅内存"是可接受的 —— 但**白屏不可接受**，所以每个访问点都兜住。
 */
function safeLocalStorage(): AuthStorageLike | null {
  try {
    if (typeof localStorage === 'undefined') return null;
    return localStorage;
  } catch {
    return null;
  }
}

const AuthContext = createContext<AuthContextValue | null>(null);

const STORAGE_KEYS = AUTH_PERSISTED_KEYS as readonly AuthPersistedKey[];

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, undefined, () => {
    // 首帧就用存储里的快照，避免"先渲染未登录、再闪一下已登录"。
    const storage = safeLocalStorage();
    const restored = storage ? restoreAuthState(storage, STORAGE_KEYS) : AUTH_SNAPSHOT_EMPTY;
    return {
      token: restored.token,
      userId: restored.userId,
      role: asUserRole(restored.role),
      username: restored.username,
      hydrated: Boolean(storage),
    };
  });

  const snapshot = useMemo<Omit<AuthState, 'hydrated'>>(
    () => ({
      token: state.token,
      userId: state.userId,
      role: state.role,
      username: state.username,
    }),
    [state.token, state.userId, state.role, state.username],
  );

  // —— 唯一持久化写路径 ——
  // 只在 `hydrated` 之后写：否则初始化那一帧的空快照会把存储里的会话抹掉
  // （这正是 T-44 时代"用 useEffect 落盘"最容易踩的坑）。
  // 也**不再**在登出时额外 clear：快照写成交集即清空，两条路径不会打架。
  useEffect(() => {
    if (!state.hydrated) return;
    const storage = safeLocalStorage();
    if (!storage) return;
    persistAuthSnapshot(
      storage,
      {
        token: snapshot.token,
        userId: snapshot.userId,
        role: snapshot.role,
        username: snapshot.username || null,
      },
      STORAGE_KEYS,
    );
  }, [state.hydrated, snapshot]);

  const restoreFromStorage = useCallback(() => {
    const storage = safeLocalStorage();
    if (!storage) return;
    const restored = restoreAuthState(storage, STORAGE_KEYS);
    dispatch({
      type: 'RESTORE',
      snapshot: {
        token: restored.token,
        userId: restored.userId,
        role: asUserRole(restored.role),
        username: restored.username,
      },
    });
  }, []);

  const signIn = useCallback((value: SignInPayload) => {
    dispatch({ type: 'SIGN_IN', value });
  }, []);

  const signOut = useCallback(() => {
    // 先把盘上的清干净（登出必须立刻生效，不能等 effect 那一拍），
    // 再让 reducer 把内存态归零 —— effect 随后写回的是同一份空快照，不会打架。
    const storage = safeLocalStorage();
    if (storage) clearAuthStorage(storage, STORAGE_KEYS);
    dispatch({ type: 'SIGN_OUT' });
  }, []);

  const applyRefreshedToken = useCallback((token: string) => {
    if (!token) return;
    // 统一层在响应处**已经**把令牌写进存储了（T-36 的 `applyRefreshedToken`）；
    // 这里只把内存态对齐，效果是"当前页面立刻用新令牌"，而不是等下次刷新。
    dispatch({ type: 'REFRESH_TOKEN', token });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      ...snapshot,
      isAuthenticated: Boolean(snapshot.token),
      isAdmin: snapshot.role === 'admin',
      hydrated: state.hydrated,
      signIn,
      signOut,
      applyRefreshedToken,
      restoreFromStorage,
    }),
    [snapshot, state.hydrated, signIn, signOut, applyRefreshedToken, restoreFromStorage],
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
