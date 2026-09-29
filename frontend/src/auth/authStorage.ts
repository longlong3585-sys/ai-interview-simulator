/**
 * T-40：认证状态的**持久化层**（唯一写路径）+ 从存储**还原会话身份**的纯逻辑。
 *
 * 为什么单独一个文件（与 T-34 的 `utils/apiBaseUrl.ts`、T-36 的 `services/authResponse.ts` 同一手法）：
 * `AuthContext.tsx` 依赖 React，`node:test` 里没法直接跑它；把"读写哪几个键、
 * 什么值算合法、登出清哪些键"这类**判断**剥出来，契约测试就能在 Node 里
 * 用假 storage **真跑一遍**"写入 → 刷新（重新还原）→ 登出清空"的闭环，
 * 而不是只靠 grep 猜源码里有没有那句话。
 *
 * 与 `services/authResponse.ts` 的分工（刻意不重叠）：
 *   · 那边 = **单个令牌**的续期写入（收到 `X-Refreshed-Token` 时）；
 *   · 这边 = **整份认证快照**（token + userId + role + username）的读写与清空。
 * 存储键的**唯一来源在本文件**（`AUTH_TOKEN_STORAGE_KEY`），
 * `authResponse` 反向转出它 —— 于是"续期写到哪一个键"与"AuthContext 读哪一个键"
 * 在结构上不可能漂移（依赖方向：`services/authResponse` → `auth/authStorage`，不成环）。
 */

/** 令牌的存储键。 */
export const AUTH_TOKEN_STORAGE_KEY = 'token';

/** userId 的存储键（T-40 新增 —— 修复前它只活在 React state 里，一刷新就没了）。 */
export const AUTH_USER_ID_STORAGE_KEY = 'userId';

/**
 * **持久化清单**：登出时要清干净的就是这 4 个键，一个都不能漏。
 *
 * 注意 `AUTH_STORAGE_KEYS` 与它的关系：前者是"认证三件套"的历史口径
 * （T-44 定的 token/role/username），现在是本清单的子集。
 * 保留导出是因为 `api-convergence.test.mjs` 断言过"续期写入的键 ∈ 认证真源"。
 */
export const AUTH_PERSISTED_KEYS = [
  AUTH_TOKEN_STORAGE_KEY,
  AUTH_USER_ID_STORAGE_KEY,
  'role',
  'username',
] as const;

export type AuthPersistedKey = (typeof AUTH_PERSISTED_KEYS)[number];

/** localStorage 的最小子集（测试里给假实现，生产给真 `localStorage`）。 */
export interface AuthStorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** 认证快照的**存储形态**（全是字符串；`userId` 是数字序列化后的结果）。 */
export interface StoredAuthSnapshot {
  token: string | null;
  userId: string | null;
  role: string | null;
  username: string | null;
}

/** 应用启动时用来还原的**内存形态**。 */
export interface RestoredAuthState {
  token: string | null;
  userId: number | null;
  role: 'admin' | 'user' | null;
  username: string;
}

/**
 * 会话身份（Bug 2 的核心）：**刷新后必须还在**。
 *
 * `null` = 真的没有会话（匿名）；只要 token 还在，userId 就该跟着回来 ——
 * 否则刷新后前端会退化成"已登录但没有身份"，任何依赖 userId 的请求都得再问一次服务端。
 *
 * `role` 的取值刻意收窄成白名单（`'admin' | 'user' | null`）而不是 `string`：
 * 存储被手工改坏时，交集类型能让 `AuthContext` 那边**编译期**就发现漏了校验。
 */
export const AUTH_SNAPSHOT_EMPTY: RestoredAuthState = {
  token: null,
  userId: null,
  role: null,
  username: '',
};

/**
 * `role` 只接受白名单取值。
 *
 * 存储是用户可改的（DevTools 里一行就能写），一个被改成 `'root'` 的 role 若直接进内存，
 * 前端守卫会做出无法解释的判断（例如"既不是 admin 也不是 user"）。这里退化成 `null`。
 */
export function parseStoredRole(raw: string | null | undefined): 'admin' | 'user' | null {
  return raw === 'admin' || raw === 'user' ? raw : null;
}

/**
 * 把存储里的 userId 字符串解析成数字。
 *
 * 刻意严格：`''` / `'abc'` / `'12abc'` / 负数 / 小数 / `NaN` 一律当成**没有身份**（`null`）。
 * 宁可退化成匿名，也不要拿一个被手工改坏的值去拼请求（那种 404/403 极难排查）。
 */
export function parseStoredUserId(raw: string | null | undefined): number | null {
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  if (!Number.isSafeInteger(value) || value <= 0) return null;
  return value;
}

/** 读一个键；存储不可用（隐私模式 / 沙箱）时返回 `null`，绝不抛。 */export function readAuthValue(storage: AuthStorageLike, key: AuthPersistedKey): string | null {
  try {
    return storage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * 写一个键；`null` = 删除该键。
 *
 * 写失败（隐私模式 / 配额满）只返回 `false`，**不抛** ——
 * `AuthContext` 的持久化写在 effect 里，抛出去会把整棵树打成白屏。
 */
export function writeAuthValue(
  storage: AuthStorageLike,
  key: AuthPersistedKey,
  value: string | null,
): boolean {
  try {
    if (value === null) storage.removeItem(key);
    else storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

/** 一次性写入整份快照（`AuthContext` 的唯一持久化出口）。 */
export function persistAuthSnapshot(
  storage: AuthStorageLike,
  snapshot: {
    token: string | null;
    userId: number | null;
    role: string | null;
    username: string | null;
  },
  keys: readonly AuthPersistedKey[] = AUTH_PERSISTED_KEYS,
): boolean {
  const values: Record<AuthPersistedKey, string | null> = {
    [AUTH_TOKEN_STORAGE_KEY]: snapshot.token,
    [AUTH_USER_ID_STORAGE_KEY]: snapshot.userId === null ? null : String(snapshot.userId),
    role: snapshot.role,
    username: snapshot.username,
  };
  let allOk = true;
  for (const key of keys) {
    if (!writeAuthValue(storage, key, values[key] ?? null)) allOk = false;
  }
  return allOk;
}

/**
 * 应用启动时**还原会话身份**（T-40 的验收标准："刷新后 `userId` 仍存在"）。
 *
 * 这一步是"乐观还原"：不校验令牌是否过期（校验要发请求，属于 T-41），
 * 因此它只回答"上次登录留下的是谁"，不回答"这个令牌还有效吗"。
 */
export function restoreAuthState(
  storage: AuthStorageLike,
  keys: readonly AuthPersistedKey[] = AUTH_PERSISTED_KEYS,
): RestoredAuthState {
  const available = new Set<string>(keys);
  const read = (key: AuthPersistedKey): string | null =>
    available.has(key) ? readAuthValue(storage, key) : null;
  return {
    token: read(AUTH_TOKEN_STORAGE_KEY),
    userId: parseStoredUserId(read(AUTH_USER_ID_STORAGE_KEY)),
    role: parseStoredRole(read('role')),
    username: read('username') ?? '',
  };
}

/**
 * 登出：把这 4 个键全部清掉（"登出清理干净"的验收标准）。
 *
 * 刻意**逐个 removeItem 且逐个 catch**：`localStorage.clear()` 会顺手删掉
 * 与本应用同源的其他数据（比如界面偏好），超出"登出"的语义边界。
 */
export function clearAuthStorage(
  storage: AuthStorageLike,
  keys: readonly AuthPersistedKey[] = AUTH_PERSISTED_KEYS,
): boolean {
  let allOk = true;
  for (const key of keys) {
    if (!writeAuthValue(storage, key, null)) allOk = false;
  }
  return allOk;
}
